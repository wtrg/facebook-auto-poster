"""Configuration loading, schema validation, and secret sanitization."""

import os
import re
import json
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from core.contracts import PostingTask, TaskStatus, WorkerTimingConfig


class ConfigValidationError(Exception):
    """Raised when configuration does not satisfy required schema or constraints."""
    def __init__(self, errors: List[str]):
        super().__init__("Configuration validation failed:\n- " + "\n- ".join(errors))
        self.errors = errors


def mask_secret(value: Optional[str], show_len: int = 4) -> str:
    """Mask sensitive string, showing only trailing or boundary characters."""
    if not value:
        return "[EMPTY]"
    v = str(value).strip()
    if len(v) <= show_len:
        return "***"
    return f"***{v[-show_len:]}"


def sanitize_text(text: str) -> str:
    """Redact sensitive tokens, cookies, and Telegram credentials from any log or error text."""
    if not text:
        return text
    # Redact Telegram bot tokens: bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
    sanitized = re.sub(r'bot(\d+:[A-Za-z0-9_-]{20,})', r'bot[REDACTED_BOT_TOKEN]', text)
    # Redact common cookie key=values: datr=..., c_user=..., xs=..., sb=...
    sanitized = re.sub(r'(c_user|xs|datr|sb|fr)=([^;\s]+)', r'\1=[REDACTED]', sanitized)
    # Redact Bearer tokens
    sanitized = re.sub(r'(Bearer\s+)[A-Za-z0-9_\-\.]{15,}', r'\1[REDACTED_TOKEN]', sanitized, flags=re.IGNORECASE)
    return sanitized


def sanitize_url(url: str) -> str:
    """Redact tokens from URLs (e.g. Telegram API endpoints)."""
    if not url:
        return url
    return re.sub(r'/bot(\d+:[A-Za-z0-9_-]+)/', r'/bot[REDACTED_TOKEN]/', url)


def validate_spintax_syntax(text: str) -> bool:
    """Verify that Spintax curly braces are properly balanced and well-formed."""
    depth = 0
    for char in text:
        if char == '{':
            depth += 1
            if depth > 10:  # Prevent absurdly nested spintax
                return False
        elif char == '}':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def is_strict_str(val: Any) -> bool:
    """Strictly string, not bool, not None, not list, not int."""
    return isinstance(val, str) and not isinstance(val, bool)


def is_strict_int(val: Any) -> bool:
    """Strictly int, not bool, not float, not str."""
    return isinstance(val, int) and not isinstance(val, bool)


def validate_config_dict(
    data: Dict[str, Any],
    base_dir: Path,
    check_media_exists: bool = True
) -> List[str]:
    """
    Validate raw config dictionary against strict schema rules.
    Returns list of error messages (empty if valid).
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        return ["Configuration root must be a JSON object."]

    tasks_raw = data.get("tasks")
    if tasks_raw is None:
        errors.append("Missing required field 'tasks'.")
    elif not isinstance(tasks_raw, list):
        errors.append("Field 'tasks' must be a list of task objects.")
    elif len(tasks_raw) == 0:
        errors.append("Field 'tasks' list cannot be empty.")
    else:
        seen_task_ids = set()
        valid_statuses = [s.value for s in TaskStatus]

        for idx, task in enumerate(tasks_raw):
            loc = f"tasks[{idx}]"
            if not isinstance(task, dict):
                errors.append(f"{loc}: Task item must be a JSON object.")
                continue

            # Task ID: strictly non-empty string
            if "task_id" not in task or not is_strict_str(task["task_id"]):
                errors.append(f"{loc}: 'task_id' must be a non-empty string.")
            elif not task["task_id"].strip():
                errors.append(f"{loc}: 'task_id' must be a non-empty string.")
            elif task["task_id"] in seen_task_ids:
                errors.append(f"{loc}: Duplicate task_id '{task['task_id']}'. Task IDs must be unique.")
            else:
                seen_task_ids.add(task["task_id"])

            tid_str = str(task.get("task_id", idx))

            # Page ID: strictly non-empty string
            if "page_id" not in task or not is_strict_str(task["page_id"]):
                errors.append(f"{loc} (task '{tid_str}'): 'page_id' must be a non-empty string.")
            elif not task["page_id"].strip():
                errors.append(f"{loc} (task '{tid_str}'): 'page_id' must be a non-empty string.")
            elif not re.match(r'^[0-9A-Za-z._-]+$', task["page_id"]):
                errors.append(f"{loc} (task '{tid_str}'): 'page_id' contains invalid characters.")

            # Content: strictly non-empty string
            if "content" not in task or not is_strict_str(task["content"]):
                errors.append(f"{loc} (task '{tid_str}'): 'content' must be non-empty text string.")
            elif not task["content"].strip():
                errors.append(f"{loc} (task '{tid_str}'): 'content' must be non-empty text string.")
            elif not validate_spintax_syntax(task["content"]):
                errors.append(f"{loc} (task '{tid_str}'): 'content' has unbalanced Spintax braces '{{}}'.")

            # Status: strictly string if provided, must be in TaskStatus
            if "status" in task:
                raw_status = task["status"]
                if not is_strict_str(raw_status) or raw_status not in valid_statuses:
                    errors.append(f"{loc} (task '{tid_str}'): Invalid status '{raw_status}'. Must be one of {valid_statuses}.")

            # Media paths: list of non-empty strings
            if "media_paths" in task:
                media_paths = task["media_paths"]
                if not isinstance(media_paths, list):
                    errors.append(f"{loc} (task '{tid_str}'): 'media_paths' must be a list of file path strings.")
                else:
                    for m_idx, mp in enumerate(media_paths):
                        if not is_strict_str(mp) or not mp.strip():
                            errors.append(f"{loc}.media_paths[{m_idx}] (task '{tid_str}'): Media path must be a non-empty string.")
                            continue
                        if check_media_exists:
                            resolved = (base_dir / mp).resolve() if not os.path.isabs(mp) else Path(mp)
                            if not resolved.exists():
                                errors.append(f"{loc} (task '{tid_str}'): Media file not found: '{mp}'.")

    # Timing validation
    if "timing" in data:
        timing = data["timing"]
        if not isinstance(timing, dict):
            errors.append("Field 'timing' must be a JSON object.")
        else:
            # min_break_minutes: strictly integer >= 1
            if "min_break_minutes" in timing:
                v = timing["min_break_minutes"]
                if not is_strict_int(v) or v <= 0:
                    errors.append("timing.min_break_minutes must be a positive integer >= 1.")

            # max_break_minutes: strictly integer >= 1
            if "max_break_minutes" in timing:
                v = timing["max_break_minutes"]
                if not is_strict_int(v) or v <= 0:
                    errors.append("timing.max_break_minutes must be a positive integer >= 1.")

            if is_strict_int(timing.get("min_break_minutes")) and is_strict_int(timing.get("max_break_minutes")):
                if timing["max_break_minutes"] < timing["min_break_minutes"]:
                    errors.append(f"timing.max_break_minutes ({timing['max_break_minutes']}) cannot be less than min_break_minutes ({timing['min_break_minutes']}).")

            # batch_size: strictly integer >= 1
            if "batch_size" in timing:
                v = timing["batch_size"]
                if not is_strict_int(v) or v <= 0:
                    errors.append("timing.batch_size must be a positive integer >= 1.")

            # max_consecutive_failures: strictly integer >= 1
            if "max_consecutive_failures" in timing:
                v = timing["max_consecutive_failures"]
                if not is_strict_int(v) or v <= 0:
                    errors.append("timing.max_consecutive_failures must be a positive integer >= 1.")

            # long_break_min_minutes: strictly integer >= 0
            if "long_break_min_minutes" in timing:
                v = timing["long_break_min_minutes"]
                if not is_strict_int(v) or v < 0:
                    errors.append("timing.long_break_min_minutes must be a non-negative integer >= 0.")

            # long_break_max_minutes: strictly integer >= 0
            if "long_break_max_minutes" in timing:
                v = timing["long_break_max_minutes"]
                if not is_strict_int(v) or v < 0:
                    errors.append("timing.long_break_max_minutes must be a non-negative integer >= 0.")

            if is_strict_int(timing.get("long_break_min_minutes")) and is_strict_int(timing.get("long_break_max_minutes")):
                if timing["long_break_max_minutes"] < timing["long_break_min_minutes"]:
                    errors.append(f"timing.long_break_max_minutes ({timing['long_break_max_minutes']}) cannot be less than long_break_min_minutes ({timing['long_break_min_minutes']}).")

    return errors


def resolve_secrets(data: Dict[str, Any], base_dir: Path) -> Dict[str, str]:
    """
    Resolve credentials strictly with precedence:
    1. Environment variables
    2. Local secret file (config/secrets.json)
    3. Input config data
    """
    secrets_file = base_dir / "secrets.json"
    local_secrets: Dict[str, str] = {}
    if secrets_file.exists():
        try:
            with open(secrets_file, "r", encoding="utf-8") as f:
                local_secrets = json.load(f)
        except Exception:
            local_secrets = {}

    cookie = (
        os.getenv("FB_ACCOUNT_COOKIE")
        or os.getenv("FACEBOOK_COOKIE")
        or local_secrets.get("account_cookie")
        or data.get("account_cookie", "")
    ).strip()

    telegram_bot_token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or local_secrets.get("telegram_bot_token")
        or data.get("telegram_bot_token", "")
    ).strip()

    telegram_chat_id = (
        os.getenv("TELEGRAM_CHAT_ID")
        or local_secrets.get("telegram_chat_id")
        or str(data.get("telegram_chat_id", ""))
    ).strip()

    return {
        "account_cookie": cookie,
        "telegram_bot_token": telegram_bot_token,
        "telegram_chat_id": telegram_chat_id
    }


def load_and_validate_config(
    config_path: Path,
    check_media_exists: bool = True
) -> Tuple[Dict[str, Any], List[PostingTask], Dict[str, str], WorkerTimingConfig]:
    """
    Read config file, validate schema, resolve secrets, and parse tasks.
    Raises ConfigValidationError if any constraint fails.
    """
    if not config_path.exists():
        raise ConfigValidationError([f"Configuration file not found: {config_path}"])

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigValidationError([f"Malformed JSON in {config_path}: {e}"])
    except Exception as e:
        raise ConfigValidationError([f"Failed reading {config_path}: {e}"])

    base_dir = config_path.parent
    errors = validate_config_dict(raw_data, base_dir=base_dir, check_media_exists=check_media_exists)
    if errors:
        raise ConfigValidationError(errors)

    secrets = resolve_secrets(raw_data, base_dir=base_dir)

    timing_dict = raw_data.get("timing", {})
    timing_cfg = WorkerTimingConfig(
        min_break_minutes=int(timing_dict.get("min_break_minutes", 3)),
        max_break_minutes=int(timing_dict.get("max_break_minutes", 6)),
        batch_size=int(timing_dict.get("batch_size", 15)),
        long_break_min_minutes=int(timing_dict.get("long_break_min_minutes", 20)),
        long_break_max_minutes=int(timing_dict.get("long_break_max_minutes", 30)),
        max_consecutive_failures=int(timing_dict.get("max_consecutive_failures", 3)),
    )

    tasks = [PostingTask.from_dict(t) for t in raw_data.get("tasks", [])]
    return raw_data, tasks, secrets, timing_cfg
