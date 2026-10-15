"""Typed contracts and domain models for Facebook Auto Poster."""

from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN_NEEDS_RECONCILIATION = "unknown_needs_reconciliation"


@dataclass
class PostingResult:
    """
    Contract for task 002 browser posting verification.
    Legacy boolean success is NOT proof of confirmed publication.
    Confirmed publication requires confirmed=True and/or verified post_id/post_url.
    """
    success: bool
    post_id: Optional[str] = None
    post_url: Optional[str] = None
    published_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    confirmed: bool = False

    def is_verified_success(self) -> bool:
        """Confirmed publication strictly requires boolean success AND confirmed flag AND valid post reference."""
        has_ref = bool(self.post_id and str(self.post_id).strip()) or bool(self.post_url and str(self.post_url).strip())
        return bool(self.success) and bool(self.confirmed) and has_ref


@dataclass
class PostingTask:
    task_id: str
    page_id: str
    page_name: str
    content: str
    media_paths: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    last_attempt_at: Optional[str] = None
    posted_at: Optional[str] = None
    post_id: Optional[str] = None
    post_url: Optional[str] = None
    error_message: Optional[str] = None
    confirmed: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, TaskStatus) else str(self.status)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PostingTask":
        raw_status = data.get("status", TaskStatus.PENDING.value)
        try:
            status = TaskStatus(raw_status)
        except ValueError:
            status = TaskStatus.UNKNOWN_NEEDS_RECONCILIATION

        return cls(
            task_id=str(data["task_id"]),
            page_id=str(data["page_id"]),
            page_name=str(data.get("page_name", data["page_id"])),
            content=str(data["content"]),
            media_paths=list(data.get("media_paths", [])),
            status=status,
            attempts=int(data.get("attempts", 0)),
            last_attempt_at=data.get("last_attempt_at"),
            posted_at=data.get("posted_at"),
            post_id=data.get("post_id"),
            post_url=data.get("post_url"),
            error_message=data.get("error_message"),
            confirmed=bool(data.get("confirmed", False)),
            metadata=dict(data.get("metadata", {}))
        )


@dataclass
class WorkerTimingConfig:
    min_break_minutes: int = 3
    max_break_minutes: int = 6
    batch_size: int = 15
    long_break_min_minutes: int = 20
    long_break_max_minutes: int = 30
    max_consecutive_failures: int = 3
