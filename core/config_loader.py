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


def validate_config_dict(
    data: Dict[str, Any],
    base_dir: Path,
    check_media_exists: bool = True
) -> List[str]:
    """
    Validate raw config dictionary against schema rules.
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
        for idx, task in enumerate(tasks_raw):
            loc = f"tasks[{idx}]"
            if not isinstance(task, dict):
                errors.append(f"{loc}: Task item must be a JSON object.")
                continue

            # Task ID
            task_id = str(task.get("task_id", "")).strip()
            if not task_id:
                errors.append(f"{loc}: 'task_id' must be a non-empty string.")
            elif task_id in seen_task_ids:
                errors.append(f"{loc}: Duplicate task_id '{task_id}'. Task IDs must be unique.")
            else:
                seen_task_ids.add(task_id)

            # Page ID
            page_id = str(task.get("page_id", "")).strip()
            if not page_id:
                errors.append(f"{loc} (task '{task_id}'): 'page_id' must be a non-empty string.")
            elif not re.match(r'^[0-9A-Za-z._-]+$', page_id):
                errors.append(f"{loc} (task '{task_id}'): 'page_id' contains invalid characters.")

            # Content
            content = task.get("content")
            if content is None or not str(content).strip():
                errors.append(f"{loc} (task '{task_id}'): 'content' must be non-empty text.")
            else:
                if not validate_spintax_syntax(str(content)):
                    errors.append(f"{loc} (task '{task_id}'): 'content' has unbalanced Spintax braces '{{}}'.")

            # Status
            raw_status = task.get("status", TaskStatus.PENDING.value)
            valid_statuses = {s.value for s in TaskStatus}
            if raw_status not in valid_statuses:
                errors.append(f"{loc} (task '{task_id}'): Invalid status '{raw_status}'. Must be one of {sorted(valid_statuses)}.")

            # Media paths
            media_paths = task.get("media_paths", [])
            if not isinstance(media_paths, list):
                errors.append(f"{loc} (task '{task_id}'): 'media_paths' must be a list of file path strings.")
            elif check_media_exists:
                for m_idx, media_path in enumerate(media_paths):
                    if not isinstance(media_path, str) or not media_path.strip():
                        errors.append(f"{loc}.media_paths[{m_idx}] (task '{task_id}'): Media path cannot be empty.")
                        continue
                    resolved = (base_dir / media_path).resolve() if not os.path.isabs(media_path) else Path(media_path)
                    if not resolved.exists():
                        errors.append(f"{loc} (task '{task_id}'): Media file not found: '{media_path}' (resolved: '{resolved}').")

    # Timing validation
    timing = data.get("timing", {})
    if isinstance(timing, dict):
        min_break = timing.get("min_break_minutes", 3)
        max_break = timing.get("max_break_minutes", 6)
        if isinstance(min_break, (int, float)) and min_break <= 0:
            errors.append("timing.min_break_minutes must be greater than 0.")
        if isinstance(min_break, (int, float)) and isinstance(max_break, (int, float)):
            if max_break < min_break:
                errors.append(f"timing.max_break_minutes ({max_break}) cannot be less than min_break_minutes ({min_break}).")

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
