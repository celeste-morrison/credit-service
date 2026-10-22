"""Per-client request rate limiting backed by Redis."""

import time

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger(__name__)

WINDOW_S = 60


class RateLimiter:
    """Fixed-window request counter per API client.

    Each client gets ``limit`` requests per minute, counted in Redis so the
    limit holds across API replicas. If Redis is unreachable the request is
    allowed: the limiter protects capacity, and refusing all traffic during a
    broker blip would cause the outage it exists to prevent.

    Args:
        redis: Async Redis client.
        limit: Requests allowed per client per window.
    """

    def __init__(self, redis: Redis, limit: int) -> None:
        self._redis = redis
        self._limit = limit

    async def retry_after(self, client_id: str) -> int | None:
        """Count one request for ``client_id``.

        Args:
            client_id: The authenticated API client.

        Returns:
            ``None`` if the request is allowed, otherwise the seconds until the
            current window ends.
        """
        now = time.time()
        window = int(now // WINDOW_S)
        key = f"ratelimit:{client_id}:{window}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, WINDOW_S * 2)
                count, _ = await pipe.execute()
        except RedisError as exc:
            log.warning("ratelimit.unavailable", error=repr(exc))
            return None
        if count <= self._limit:
            return None
        return max(1, int((window + 1) * WINDOW_S - now))
