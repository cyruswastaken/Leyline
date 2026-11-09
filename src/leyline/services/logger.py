import logging
from datetime import UTC, datetime
from typing import Any

from leyline.config import get_settings

LOG_DIR = get_settings().log_dir

# Create logs directory if it doesn't exist
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Security log file
LOG_FILE = LOG_DIR / "security_events.log"


logger = logging.getLogger("secure_helpdesk")
logger.setLevel(logging.INFO)


# Prevent duplicate handlers when Flask reloads
if not logger.handlers:
    # Terminal handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    # File handler
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")

    file_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


def log_security_event(
    event_type: str,
    user_id: str,
    status_code: int,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Log a structured security or system event to both
    the terminal and persistent security log file.
    """

    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        "user_id": user_id,
        "status_code": status_code,
        "reason": reason,
    }

    if details:
        event["details"] = details

    logger.info(event)
