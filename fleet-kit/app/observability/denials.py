"""Bounded auditing of refused requests (denial-flood protection).

Since T09 every refused request (401/403/404/409) was its own audit entry.
Anyone could then grow the audit table at request rate just by sending bad
keys, and every entry also takes the audit write lock (BEGIN IMMEDIATE), so
a flood slowed down every legitimate write too.

DenialLimiter decides, per refused request, whether it gets an individual
audit entry or is only counted. Counted requests are written as one
request.denied.summary entry per bucket when its time window closes.

Buckets (per fixed window of window_seconds):
- unauthenticated requests: one bucket per source address, at most
  max_sources buckets; requests from further sources share one overflow
  bucket that is never logged individually;
- everything else (a known tenant, an agent, or a refused agent key): one
  bucket per principal. Principals come from configured keys, so there are
  finitely many of them.

Individual entries: at most per_source per unauthenticated bucket, at most
per_principal per principal bucket, and at most max_individual per window
in total. Hard bound per window, whatever an attacker does (any number of
requests, any number of spoofed or rotated sources):

    entries <= max_individual + summaries
    summaries <= max_sources + 1 + (number of principals that were refused)

The decision never rejects a request; it only decides how it is recorded.
Rate limiting the requests themselves (429) is deliberately not done here:
behind a reverse proxy every client shares the proxy's address.

Counts live in memory: suppressed counts of the current window are lost if
the process dies before the window is flushed (individual entries are
durable). Each server process has its own limiter, so with N worker
processes the bound is N times larger.

No FastAPI imports: the module is pure logic so it can be tested alone.
"""
import math
import threading
import time
from collections import Counter
from datetime import datetime, timezone

OVERFLOW_SOURCE = "*overflow*"
SOURCES_SAMPLE = 5


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class _Bucket:
    def __init__(self, actor, principal, source, overflow=False):
        self.actor = actor
        self.principal = principal
        self.source = source
        self.overflow = overflow
        self.individual = 0
        self.suppressed = 0
        self.statuses = Counter()
        self.routes = Counter()
        self.sources = []


class DenialLimiter:
    def __init__(
        self,
        *,
        window_seconds: int = 60,
        per_source: int = 1,
        per_principal: int = 5,
        max_sources: int = 20,
        max_individual: int = 100,
        clock=time.time,
    ):
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        for name, value in (
            ("per_source", per_source),
            ("per_principal", per_principal),
            ("max_sources", max_sources),
            ("max_individual", max_individual),
        ):
            if value < 0:
                raise ValueError(f"{name} must be >= 0")
        self.window_seconds = window_seconds
        self.per_source = per_source
        self.per_principal = per_principal
        self.max_sources = max_sources
        self.max_individual = max_individual
        self.clock = clock
        self._lock = threading.Lock()
        self._clear(None)

    def _clear(self, window_start):
        self._window_start = window_start
        self._buckets = {}
        self._sources = 0
        self._individual = 0

    def reset(self) -> None:
        """Forget all counts (tests). Suppressed counts are dropped, not flushed."""
        with self._lock:
            self._clear(None)

    def decide(self, *, actor: str, principal: dict, source: str, status: int, route: str):
        """Record one refused request.

        Returns (write_individual, summaries): write the request.denied entry
        only when write_individual is True; write every summary in summaries
        (they belong to windows that have just closed) in either case.
        """
        unauthenticated = principal.get("kind") is None
        now = self.clock()
        with self._lock:
            summaries = self._roll(now, force=False)
            if unauthenticated:
                key = ("unauthenticated", source)
            else:
                key = (actor, principal.get("kind"), principal.get("agent_id"))
            bucket = self._buckets.get(key)
            if bucket is None:
                if unauthenticated and self._sources >= self.max_sources:
                    key = ("unauthenticated", OVERFLOW_SOURCE)
                    bucket = self._buckets.get(key)
                    if bucket is None:
                        bucket = _Bucket(actor, principal, OVERFLOW_SOURCE, overflow=True)
                        self._buckets[key] = bucket
                else:
                    bucket = _Bucket(actor, principal, source if unauthenticated else None)
                    self._buckets[key] = bucket
                    if unauthenticated:
                        self._sources += 1
            if not unauthenticated and source not in bucket.sources and len(bucket.sources) < SOURCES_SAMPLE:
                bucket.sources.append(source)
            if bucket.overflow:
                budget = 0
            else:
                budget = self.per_source if unauthenticated else self.per_principal
            if bucket.individual < budget and self._individual < self.max_individual:
                bucket.individual += 1
                self._individual += 1
                return True, summaries
            bucket.suppressed += 1
            bucket.statuses[str(status)] += 1
            bucket.routes[route] += 1
            return False, summaries

    def flush(self, force: bool = False) -> list:
        """Summaries of windows that have closed (also with no new denial).
        force=True also closes the current window (shutdown)."""
        with self._lock:
            return self._roll(self.clock(), force=force)

    def _roll(self, now: float, force: bool) -> list:
        window_start = math.floor(now / self.window_seconds) * self.window_seconds
        if self._window_start is None:
            self._window_start = window_start
            return []
        if window_start == self._window_start and not force:
            return []
        start = self._window_start
        end = now if force else start + self.window_seconds
        out = [
            self._summary(b, start, end, force)
            for b in self._buckets.values()
            if b.suppressed
        ]
        self._clear(window_start)
        return out

    def _summary(self, b: _Bucket, start: float, end: float, early: bool) -> dict:
        detail = {
            "window_start": _iso(start),
            "window_end": _iso(end),
            "flushed_early": early,
            "suppressed": b.suppressed,
            "logged_individually": b.individual,
            "statuses": dict(sorted(b.statuses.items())),
            "routes": dict(sorted(b.routes.items())),
            "principal": b.principal,
            "overflow": b.overflow,
        }
        if b.principal.get("kind") is None:
            detail["source"] = b.source
        else:
            detail["sources_sample"] = list(b.sources)
        return {"actor": b.actor, "target": f"window:{_iso(start)}", "detail": detail}
