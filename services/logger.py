import logging
from datetime import datetime, timezone
from pathlib import Path


# Project root directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Logs directory
LOG_DIR = BASE_DIR / "logs"

# Create logs directory if it doesn't exist
LOG_DIR.mkdir(exist_ok=True)

# Security log file
LOG_FILE = LOG_DIR / "security_events.log"


logger = logging.getLogger("secure_helpdesk")
logger.setLevel(logging.INFO)


# Prevent duplicate handlers when Flask reloads
if not logger.handlers:

    # Terminal handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(message)s")
    )

    # File handler
    file_handler = logging.FileHandler(
        LOG_FILE,
        encoding="utf-8"
    )

    file_handler.setFormatter(
        logging.Formatter("%(message)s")
    )

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


def log_security_event(
    event_type: str,
    user_id: str,
    status_code: int,
    reason: str
) -> None:
    """
    Log a structured security event to both the terminal
    and the persistent security log file.
    """

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "user_id": user_id,
        "status_code": status_code,
        "reason": reason
    }

    logger.info(event)