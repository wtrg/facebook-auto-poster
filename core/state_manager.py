"""Atomic persistence, single-worker mutual exclusion, and crash recovery."""

import os
import sys
import json
import uuid
import time
import socket
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from core.contracts import PostingTask, TaskStatus, PostingResult


class ConcurrentWorkerError(Exception):
    """Raised when another worker process holds the state lock."""
    pass


class PersistenceError(Exception):
    """Raised when atomic state persistence fails."""
    pass


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently alive on the host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if handle == 0:
            return False
        # Process handle is open, check if still active
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        STILL_ACTIVE = 259
        return exit_code.value == STILL_ACTIVE
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


class SingleWorkerLock:
    """
    Mutual exclusion lock to ensure single-worker ownership of a state store.
    Uses atomic file creation with PID tracking and stale lock breaking.
    """
    def __init__(self, lock_file: Path, stale_timeout_seconds: float = 300.0):
        self.lock_file = lock_file
        self.stale_timeout_seconds = stale_timeout_seconds
        self.fd: Optional[int] = None
        self._acquired = False

    def acquire(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        my_pid = os.getpid()
        hostname = socket.gethostname()

        for attempt in range(2):
            try:
                # Atomic file creation
                self.fd = os.open(
                    str(self.lock_file),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR
                )
                payload = {
                    "pid": my_pid,
                    "hostname": hostname,
                    "acquired_at": datetime.now(timezone.utc).isoformat()
                }
                os.write(self.fd, json.dumps(payload, indent=2).encode("utf-8"))
                os.fsync(self.fd)
                self._acquired = True
                return
            except FileExistsError:
                # Inspect existing lock
                stale = False
                existing_pid = None
                try:
                    with open(self.lock_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        existing_pid = data.get("pid")
                except Exception:
                    stale = True

                if existing_pid:
                    if not is_pid_alive(existing_pid):
                        stale = True
                    else:
                        raise ConcurrentWorkerError(
                            f"Another worker process (PID {existing_pid} on {hostname}) "
                            f"currently holds the state lock at '{self.lock_file}'."
                        )

                if stale and attempt == 0:
                    try:
                        self.lock_file.unlink(missing_ok=True)
                        time.sleep(0.05)
                        continue
                    except Exception:
                        pass

                raise ConcurrentWorkerError(
                    f"Unable to acquire single-worker lock at '{self.lock_file}'. Existing lock active."
                )

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

        if self._acquired:
            try:
                self.lock_file.unlink(missing_ok=True)
            except Exception:
                pass
            self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AtomicStateStore:
    """
    Manages persistent state for tasks separate from shareable configuration.
    Ensures:
    1. Atomic commit via write-flush-fsync-replace.
    2. No loss of previously committed state.
    3. Crash recovery: any IN_PROGRESS task becomes UNKNOWN_NEEDS_RECONCILIATION.
    4. Corrupt state fails closed without overwriting.
    5. Archived tasks not in current config are not scheduled.
    """
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.tasks: Dict[str, PostingTask] = {}
        self.configured_task_ids: set[str] = set()
        self.metadata: Dict[str, Any] = {}

    def load_or_initialize(
        self,
        configured_tasks: List[PostingTask],
        read_only: bool = False
    ) -> int:
        """
        Load existing state or initialize from configured tasks.
        Recovers crashed in-progress work as UNKNOWN_NEEDS_RECONCILIATION.
        When read_only is True, state is inspected without writing to disk.
        Returns number of recovered tasks.
        """
        recovered_count = 0
        existing_tasks: Dict[str, PostingTask] = {}
        self.configured_task_ids = {t.task_id for t in configured_tasks}

        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                raise PersistenceError(f"Failed to read existing state file '{self.state_file}': {e}")

            # Corrupt existing state checks
            if not isinstance(data, dict):
                raise PersistenceError(f"Existing state file '{self.state_file}' is not a valid JSON object.")
            if "tasks" not in data or not isinstance(data["tasks"], list):
                raise PersistenceError(f"Existing state file '{self.state_file}' is missing a valid 'tasks' list.")
            if len(data["tasks"]) == 0 and len(configured_tasks) > 0:
                raise PersistenceError(f"Existing state file '{self.state_file}' has empty 'tasks' list; cannot reset configured tasks.")

            self.metadata = dict(data.get("metadata", {}))
            seen_state_ids = set()
            valid_statuses = [s.value for s in TaskStatus]

            for t_data in data["tasks"]:
                if not isinstance(t_data, dict) or "task_id" not in t_data:
                    raise PersistenceError(f"Existing state file '{self.state_file}' contains invalid task item.")

                tid = t_data["task_id"]
                if tid in seen_state_ids:
                    raise PersistenceError(f"Duplicate task record '{tid}' in existing state file '{self.state_file}'.")
                seen_state_ids.add(tid)

                if "status" not in t_data or not t_data["status"] or t_data["status"] not in valid_statuses:
                    raise PersistenceError(f"Task '{tid}' in existing state is missing a valid 'status'.")

                task = PostingTask.from_dict(t_data)
                # Crash recovery check: if task was left in_progress, previous run crashed mid-flight
                if task.status == TaskStatus.IN_PROGRESS:
                    recovered_count += 1
                    if not read_only:
                        task.status = TaskStatus.UNKNOWN_NEEDS_RECONCILIATION
                        task.error_message = (
                            "Process interrupted or crashed while in-flight. "
                            "Outcome uncertain; manual reconciliation required before retry."
                        )
                existing_tasks[task.task_id] = task

        # Merge configured tasks into state without losing committed state
        self.tasks = {}
        for cfg_task in configured_tasks:
            tid = cfg_task.task_id
            if tid in existing_tasks:
                existing = existing_tasks[tid]
                identity_changed = (existing.content != cfg_task.content) or (existing.page_id != cfg_task.page_id)
                if identity_changed and (existing.attempts > 0 or existing.status != TaskStatus.PENDING):
                    if not read_only:
                        existing.status = TaskStatus.UNKNOWN_NEEDS_RECONCILIATION
                        existing.error_message = (
                            "Task content or page_id changed after prior execution/attempt; "
                            "manual reconciliation required."
                        )
                else:
                    existing.page_name = cfg_task.page_name
                    existing.content = cfg_task.content
                    existing.media_paths = cfg_task.media_paths
                self.tasks[tid] = existing
            else:
                self.tasks[tid] = cfg_task

        # Retain archived history for any tasks that were in state but removed from current config
        for tid, ex_task in existing_tasks.items():
            if tid not in self.tasks:
                self.tasks[tid] = ex_task

        # Persist recovered state only when not read_only
        if not read_only:
            self.save()

        return recovered_count

    def save(self) -> None:
        """Atomically persist state to disk."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.state_file.parent / f".tmp_{self.state_file.name}_{uuid.uuid4().hex}"

        payload = {
            "version": "1.0.0",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "metadata": self.metadata,
            "tasks": [t.to_dict() for t in self.tasks.values()]
        }

        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise PersistenceError(f"Atomic persistence failed for '{self.state_file}': {e}")

    def mark_in_progress(self, task_id: str) -> PostingTask:
        """
        Record in-flight state BEFORE side effect.
        Aborts if persistence fails before side effect attempt.
        """
        task = self.tasks[task_id]
        task.status = TaskStatus.IN_PROGRESS
        task.attempts += 1
        task.last_attempt_at = datetime.now(timezone.utc).isoformat()
        self.save()
        return task

    def mark_completed(
        self,
        task_id: str,
        post_id: Optional[str] = None,
        post_url: Optional[str] = None,
        confirmed: bool = True
    ) -> PostingTask:
        """Mark task as successfully published with verified reference."""
        has_ref = bool(post_id and str(post_id).strip()) or bool(post_url and str(post_url).strip())
        if not (bool(confirmed) and has_ref):
            raise ValueError("Completion strictly requires confirmed=True and a valid post reference (post_id or post_url).")
        task = self.tasks[task_id]
        task.status = TaskStatus.COMPLETED
        task.posted_at = datetime.now(timezone.utc).isoformat()
        task.post_id = post_id
        task.post_url = post_url
        task.confirmed = True
        task.error_message = None
        self.save()
        return task

    def mark_failed(self, task_id: str, error_message: str) -> PostingTask:
        """Mark task as failed with recorded error."""
        task = self.tasks[task_id]
        task.status = TaskStatus.FAILED
        task.error_message = error_message
        self.save()
        return task

    def mark_unknown_reconciliation(self, task_id: str, reason: str) -> PostingTask:
        """Mark task as uncertain outcome requiring reconciliation."""
        task = self.tasks[task_id]
        task.status = TaskStatus.UNKNOWN_NEEDS_RECONCILIATION
        task.error_message = reason
        self.save()
        return task

    def get_pending_tasks(self) -> List[PostingTask]:
        """Return only currently configured, authorized tasks eligible for posting (status == PENDING)."""
        return [
            t for t in self.tasks.values()
            if t.task_id in self.configured_task_ids and t.status == TaskStatus.PENDING
        ]

    def get_summary(self) -> Dict[str, int]:
        """Return task count breakdown by status."""
        summary = {s.value: 0 for s in TaskStatus}
        for t in self.tasks.values():
            val = t.status.value if isinstance(t.status, TaskStatus) else str(t.status)
            summary[val] = summary.get(val, 0) + 1
        return summary
