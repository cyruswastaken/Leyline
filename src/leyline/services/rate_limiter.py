import threading
import time

from leyline.config import get_settings

_settings = get_settings()

RATE_LIMIT = _settings.rate_limit_requests
WINDOW_SECONDS = _settings.rate_limit_window_seconds


class RateLimiter:
    """In-process sliding-window limiter.

    Single-instance only; replaced by a Redis-backed limiter in Phase 1.
    """

    def __init__(self) -> None:
        self.requests: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, user_id: str) -> bool:
        current_time = time.time()

        with self._lock:
            recent = [
                timestamp
                for timestamp in self.requests.get(user_id, [])
                if current_time - timestamp < WINDOW_SECONDS
            ]

            if len(recent) >= RATE_LIMIT:
                self.requests[user_id] = recent
                return False

            recent.append(current_time)
            self.requests[user_id] = recent

            return True


rate_limiter = RateLimiter()
