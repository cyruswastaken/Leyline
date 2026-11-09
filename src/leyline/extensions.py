"""Typed accessors for per-application state registered by ``create_app``."""

from __future__ import annotations

from typing import cast

from flask import current_app
from redis import Redis

from leyline.config import Settings
from leyline.ratelimit import RedisRateLimiter

SETTINGS_KEY = "leyline.settings"
REDIS_KEY = "leyline.redis"
RATE_LIMITER_KEY = "leyline.rate_limiter"


def app_settings() -> Settings:
    return cast(Settings, current_app.extensions[SETTINGS_KEY])


def redis_client() -> Redis:
    return cast(Redis, current_app.extensions[REDIS_KEY])


def rate_limiter() -> RedisRateLimiter:
    return cast(RedisRateLimiter, current_app.extensions[RATE_LIMITER_KEY])
