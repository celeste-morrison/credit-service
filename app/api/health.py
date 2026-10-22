"""Liveness and readiness probes."""

import asyncio
import time
from collections.abc import Awaitable, Callable

type Check = Callable[[], Awaitable[object]]


class Readiness:
    """Runs dependency checks, caching the result briefly.

    Probes are unauthenticated, so without caching every request would open
    fresh MongoDB and Redis round-trips. Concurrent callers share one in-flight
    check and its cached result for ``ttl_s`` seconds.

    Args:
        checks: Named async checks; each raises if its dependency is down.
        ttl_s: How long a result is reused.
        timeout_s: Time limit for each check.
    """

    def __init__(
        self, checks: dict[str, Check], ttl_s: float = 1.0, timeout_s: float = 2.0
    ) -> None:
        self._checks = checks
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s
        self._lock = asyncio.Lock()
        self._cached: dict[str, str] | None = None
        self._checked_at = 0.0

    async def results(self) -> dict[str, str]:
        """Run the checks, or reuse the result if it is younger than ``ttl_s``.

        Returns:
            Check name mapped to ``"ok"`` or ``"error: <ExceptionType>"``.
        """
        async with self._lock:
            if self._cached is None or time.monotonic() - self._checked_at >= self._ttl_s:
                outcomes = await asyncio.gather(*(self._run(c) for c in self._checks.values()))
                self._cached = dict(zip(self._checks, outcomes, strict=True))
                self._checked_at = time.monotonic()
            return self._cached

    async def _run(self, check: Check) -> str:
        """Run one check under the timeout and summarise its outcome."""
        try:
            await asyncio.wait_for(check(), self._timeout_s)
        except Exception as exc:
            return f"error: {type(exc).__name__}"
        return "ok"
