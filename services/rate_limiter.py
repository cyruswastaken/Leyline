import time


RATE_LIMIT = 10
WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self):
        self.requests = {}

    def is_allowed(self, user_id: str) -> bool:
        current_time = time.time()

        if user_id not in self.requests:
            self.requests[user_id] = []

        self.requests[user_id] = [
            timestamp
            for timestamp in self.requests[user_id]
            if current_time - timestamp < WINDOW_SECONDS
        ]

        if len(self.requests[user_id]) >= RATE_LIMIT:
            return False

        self.requests[user_id].append(current_time)

        return True


rate_limiter = RateLimiter()