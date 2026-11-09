"""Distributed sliding-window rate limiting backed by Redis.

State lives in Redis, so limits hold across gunicorn workers and across
multiple app instances (the old in-process limiter did neither).
"""

from __future__ import annotations

import secrets
import time

from redis import Redis

_KEY_PREFIX = "leyline:ratelimit:"


class RedisRateLimiter:
    def __init__(self, client: Redis) -> None:
        self.client = client

    def is_allowed(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """Record a hit for ``key``; return False if it exceeds the limit.

        Rejected hits are removed again so that a client who keeps retrying
        while blocked does not extend its own lockout.
        """
        now = time.time()
        redis_key = _KEY_PREFIX + key
        member = f"{now:.6f}:{secrets.token_hex(4)}"

        pipe = self.client.pipeline(transaction=True)
        pipe.zremrangebyscore(redis_key, 0, now - window_seconds)
        pipe.zadd(redis_key, {member: now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window_seconds + 1)
        results = pipe.execute()

        if int(results[2]) > limit:
            self.client.zrem(redis_key, member)
            return False
        return True
