"""Brute-force protection for API keys: authentication-failure backoff (B2).

DenialLimiter (app/observability/denials.py) only bounds how refused requests
are AUDITED and never refuses or slows a request. This module is the part
that does refuse: after repeated authentication failures it answers 429 (with
Retry-After) before the presented key is even checked.

The constraint from the denial-flood patch still holds: behind a reverse
proxy every client has the proxy's address, so "N failures from this address
-> 429 for this address" would let one attacker lock out every legitimate
client behind the same proxy. The design below makes that impossible.

Two scopes, both counted only on a failed check of a PRESENTED key (a
missing X-API-Key is not a guess and is not counted; a request refused with
429 is not checked and therefore not counted either):

1. Credential scope - keyed by a fingerprint of the presented value
   (HMAC-SHA256 under a random per-process secret; never the key itself and
   never the configured hash). After per_credential consecutive failures of
   the same value it is refused for backoff_base seconds, then each further
   failure doubles the wait up to backoff_max. A success resets it.
   Lock-out safety: an entry only accumulates failures while that value
   FAILS. A legitimate client's key succeeds, so nobody - attacker or not -
   can push a working key into backoff. This scope stops a single client
   (or a revoked/suspended agent) from hammering with the same wrong key.

2. Source scope - keyed by the direct peer address (X-Forwarded-For is not
   trusted: it is client-controlled, so an attacker could rotate it per
   request to escape the limit or put a victim's address in it). After
   per_source failures in window_seconds the source is in backoff (same
   progressive schedule). While it is, a presented key from that source is
   refused with 429 WITHOUT being checked - unless its fingerprint is
   "known good": it authenticated successfully within known_good_ttl.
   Lock-out safety on a shared proxy: clients that were already working
   keep working (their key is known good, and a known-good key cannot be
   forged without knowing the key). The cost of a block falls only on
   credentials that have not proven themselves - which is where every
   guess is - and is bounded: the block lifts on its own after at most
   backoff_max, nothing is permanent, no operator unlock is needed.
   A success does NOT reset the source scope: on a shared proxy it would
   let a legitimate client's traffic launder the attacker's failures.
   per_source defaults high (it is the proxy's budget for all its clients);
   a Hub that is reached directly, without a proxy, can lower it.

Fail-closed reasoning:
- During a block the key is not evaluated at all, so the attacker gets no
  answer (no oracle) and the Hub spends no work on the guess; "refuse, then
  retry later" is the safe side of every decision here.
- Unknown or unproven credentials are the ones refused under doubt; proven
  ones are the only exception, and only for the source scope (a credential
  in its own backoff is refused even if it was known good: it has failed
  since, and a failure removes it from the known-good set).
- The wait is pushed to the client with Retry-After. Server-side sleeping
  ("tarpit") is deliberately not used: a sleeping request holds a worker
  thread, so an attacker could exhaust the pool and stall legitimate
  clients - the opposite of fail-closed.
- Memory is bounded (max_tracked entries per table, least recently touched
  dropped first). An attacker who floods new values only evicts idle
  entries; its own source entry is touched on every failure, so it stays.

Limits, stated rather than hidden: state is per process and in memory (N
worker processes -> N times the budget; a restart forgets all counts). The
primary defence against guessing remains key entropy: issued keys carry 256
random bits (scripts/gen_keys.py); this limiter makes online guessing of a
weak or leaked-pattern key slow and visible (429s are audited, bounded by
DenialLimiter).

No FastAPI imports: pure logic, testable alone with a fake clock.
"""
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import OrderedDict

logger = logging.getLogger("agent_fleet_hub.identity.attempts")

MAX_STRIKES = 30  # caps the exponent; backoff_max caps the wait anyway


class _Entry:
    __slots__ = ("failures", "window_start", "last_failure", "strikes", "blocked_until")

    def __init__(self, now: float):
        self.failures = 0
        self.window_start = now
        self.last_failure = now
        self.strikes = 0
        self.blocked_until = 0.0


class AuthAttemptLimiter:
    def __init__(
        self,
        *,
        window_seconds: int = 300,
        per_credential: int = 5,
        per_source: int = 500,
        backoff_base_seconds: int = 1,
        backoff_max_seconds: int = 300,
        known_good_ttl_seconds: int = 86400,
        max_tracked: int = 10000,
        clock=time.monotonic,
    ):
        for name, value, minimum in (
            ("window_seconds", window_seconds, 1),
            ("per_credential", per_credential, 1),
            ("per_source", per_source, 1),
            ("backoff_base_seconds", backoff_base_seconds, 1),
            ("backoff_max_seconds", backoff_max_seconds, 1),
            ("known_good_ttl_seconds", known_good_ttl_seconds, 1),
            ("max_tracked", max_tracked, 1),
        ):
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        self.window_seconds = window_seconds
        self.per_credential = per_credential
        self.per_source = per_source
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.known_good_ttl_seconds = known_good_ttl_seconds
        self.max_tracked = max_tracked
        self.clock = clock
        # Per-process secret for fingerprints: they cannot be reversed, are
        # not the configured hashes, and mean nothing outside this process.
        self._secret = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._clear()

    def _clear(self) -> None:
        self._credentials = OrderedDict()
        self._sources = OrderedDict()
        self._known_good = OrderedDict()

    def reset(self) -> None:
        """Forget all state (tests)."""
        with self._lock:
            self._clear()

    # ------------------------------------------------------------ public

    def fingerprint(self, realm: str, presented: str) -> str:
        """Opaque id of a presented value within a realm ("client" for
        tenant/agent keys, "operator" for the operator key), so failures on
        one kind of endpoint never back off a key on the other."""
        msg = realm.encode("utf-8") + b"\x00" + presented.encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:32]

    def check(self, fingerprint: str, source: str) -> float:
        """Seconds the caller must wait before this attempt is evaluated;
        0 means go ahead and check the key."""
        now = self.clock()
        with self._lock:
            cred = self._live(self._credentials, fingerprint, now)
            if cred is not None and now < cred.blocked_until:
                return cred.blocked_until - now
            if self._is_known_good(fingerprint, now):
                return 0.0
            src = self._live(self._sources, source, now)
            if src is not None and now < src.blocked_until:
                return src.blocked_until - now
            return 0.0

    def failure(self, fingerprint: str, source: str) -> None:
        """A presented key was checked and refused."""
        now = self.clock()
        with self._lock:
            self._known_good.pop(fingerprint, None)
            cred_block = self._fail(self._credentials, fingerprint, now, self.per_credential)
            src_block = self._fail(self._sources, source, now, self.per_source)
        # The presented key is never logged; neither is its fingerprint.
        if cred_block:
            logger.warning("auth backoff: a presented credential is refused for %ds", cred_block)
        if src_block:
            logger.warning("auth backoff: unproven credentials from %s refused for %ds", source, src_block)

    def success(self, fingerprint: str) -> None:
        """A presented key was checked and accepted."""
        now = self.clock()
        with self._lock:
            self._credentials.pop(fingerprint, None)
            self._known_good[fingerprint] = now
            self._known_good.move_to_end(fingerprint)
            self._trim(self._known_good)

    # ------------------------------------------------------------ internals

    def _is_known_good(self, fingerprint: str, now: float) -> bool:
        seen = self._known_good.get(fingerprint)
        if seen is None:
            return False
        if now - seen > self.known_good_ttl_seconds:
            del self._known_good[fingerprint]
            return False
        return True

    def _live(self, table: OrderedDict, key: str, now: float):
        """The entry for key, or None once it has been forgiven: not blocked
        and no failure for a whole window."""
        entry = table.get(key)
        if entry is None:
            return None
        if now >= entry.blocked_until and now - entry.last_failure >= self.window_seconds:
            del table[key]
            return None
        return entry

    def _fail(self, table: OrderedDict, key: str, now: float, threshold: int) -> int:
        """Count one failure; returns the new block length in seconds, or 0."""
        entry = self._live(table, key, now)
        if entry is None:
            entry = _Entry(now)
            table[key] = entry
        table.move_to_end(key)
        self._trim(table)
        if now - entry.window_start >= self.window_seconds:
            entry.failures = 0
            entry.window_start = now
        entry.failures += 1
        entry.last_failure = now
        # Before the first block: threshold failures per window. After a
        # block (until forgiven): every failure blocks again, twice as long.
        limit = 1 if entry.strikes else threshold
        if entry.failures < limit:
            return 0
        entry.strikes = min(entry.strikes + 1, MAX_STRIKES)
        wait = min(self.backoff_base_seconds * 2 ** (entry.strikes - 1), self.backoff_max_seconds)
        entry.blocked_until = now + wait
        entry.failures = 0
        entry.window_start = now
        return wait

    def _trim(self, table: OrderedDict) -> None:
        while len(table) > self.max_tracked:
            table.popitem(last=False)
