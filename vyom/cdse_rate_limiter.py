"""
cdse_rate_limiter -- every CDSE HTTP call (discovery search + download) should
go through cdse_request() instead of calling requests directly. Coordinates
across separate Celery worker processes via Redis, against CDSE's documented
limits (https://documentation.dataspace.copernicus.eu/Quotas.html):

  - Concurrent connections (IAD data): 4 -- we use 3, leaving headroom for
    token refresh + manual scripts (check_cdse_auth.py) on the same account.
  - Requests/minute: table lists "2000" against a merged column footnoted
    "only applies to S3" -- OData's real number is undocumented, so this
    throttles conservatively (120/min default) rather than assume it's high.

DESIGN NOTES (v2 -- production hardening pass):

Slot tracking is a Redis SORTED SET of LEASES (member=unique lease id, score=
expiry unix time), not a plain INCR/DECR counter. This fixes a real
correctness bug in v1: GET-then-conditional-INCR across two separate Python
calls is NOT atomic even inside a redis-py pipeline (pipeline() without WATCH
just batches commands -- it doesn't make read-then-act atomic). Two workers
could both read count=2 (under max=3) and both increment, landing at 4 -- over
the limit. The acquire/renew/release operations here are single Lua scripts,
which Redis executes atomically server-side, closing that race.

A held slot has a lease TTL (default 90s) that the caller must renew via
renew_slot_lease() while a long operation (a multi-minute streaming download)
is still in progress. This replaces v1's "reset a shared TTL on every acquire
attempt" approach, which could expire a slot still legitimately in use if no
other worker happened to poll during a quiet stretch -- each lease now has its
OWN expiry, unaffected by other workers' activity, and is only ever extended
by its actual owner calling renew.

Fairness: waiters register in a separate ZSET (score=enqueue time) and are
only granted a slot once they're the oldest waiter with a slot free -- avoids
a live waiter being starved indefinitely by later arrivals winning a race.
"""
import logging
import time
import uuid

import redis
import requests

from vyom.auth_broker import auth_broker
from vyom.config import settings
from vyom.error_log import log_error

logger = logging.getLogger("vyom.cdse_rate_limiter")

MAX_CONCURRENT_CONNECTIONS = settings.cdse_max_concurrent_connections
MAX_REQUESTS_PER_MINUTE = settings.cdse_max_requests_per_minute

# a held slot's lease length; renew before this to keep holding it
LEASE_TTL_SECONDS = 90
# a queued waiter older than this is presumed dead, pruned
WAITER_STALE_SECONDS = 180
# generous default -- was 60s in v1, too tight under real contention
DEFAULT_ACQUIRE_TIMEOUT = 180

_MAX_RETRIES = 5
_BASE_BACKOFF_SECONDS = 2

_redis = redis.from_url(settings.redis_url, decode_responses=True)

# ZSET member=lease_id, score=expiry
_ACTIVE_SLOTS_KEY = "cdse:active_slots"
# ZSET member=waiter_id, score=enqueue_time
_WAITING_QUEUE_KEY = "cdse:waiting_queue"
_RATE_WINDOW_KEY = "cdse:request_timestamps"

_STAT_WAIT_SECONDS = "cdse:stats:total_wait_seconds"
_STAT_429_COUNT = "cdse:stats:429_count"
_STAT_ACQUIRE_TIMEOUT_COUNT = "cdse:stats:acquire_timeout_count"

# Atomic try-acquire: prunes expired leases + stale waiters, registers this
# waiter if new, and only grants a slot if one is free AND this waiter is the
# oldest in queue (fairness) -- all in one round trip, no read-then-act race.
_TRY_ACQUIRE_SCRIPT = """
local active_key = KEYS[1]
local waiting_key = KEYS[2]
local now = tonumber(ARGV[1])
local max_concurrent = tonumber(ARGV[2])
local lease_ttl = tonumber(ARGV[3])
local waiter_id = ARGV[4]
local stale_wait = tonumber(ARGV[5])

redis.call('ZREMRANGEBYSCORE', active_key, '-inf', now)
redis.call('ZREMRANGEBYSCORE', waiting_key, '-inf', now - stale_wait)
redis.call('ZADD', waiting_key, 'NX', now, waiter_id)

local active_count = redis.call('ZCARD', active_key)
local rank = redis.call('ZRANK', waiting_key, waiter_id)

if active_count < max_concurrent and rank == 0 then
    redis.call('ZREM', waiting_key, waiter_id)
    redis.call('ZADD', active_key, now + lease_ttl, waiter_id)
    return 1
else
    return 0
end
"""

_RENEW_LEASE_SCRIPT = """
local active_key = KEYS[1]
local now = tonumber(ARGV[1])
local lease_ttl = tonumber(ARGV[2])
local lease_id = ARGV[3]

if redis.call('ZSCORE', active_key, lease_id) then
    redis.call('ZADD', active_key, now + lease_ttl, lease_id)
    return 1
else
    return 0
end
"""

_RELEASE_SCRIPT = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""

_try_acquire = _redis.register_script(_TRY_ACQUIRE_SCRIPT)
_renew_lease = _redis.register_script(_RENEW_LEASE_SCRIPT)
_release = _redis.register_script(_RELEASE_SCRIPT)


class CdseRateLimitExceeded(Exception):
    """Raised when a connection slot couldn't be acquired within the timeout,
    or CDSE itself returned 429 after all retries. Caller-visible signal to
    back off -- callers should treat this as transient/expected congestion
    (log at warning, not error) rather than a real bug."""


class CdseLeaseLost(Exception):
    """Raised by renew_slot_lease() if the lease expired before being
    renewed (e.g. a download took longer than LEASE_TTL_SECONDS without
    renewing in time, or the Redis entry was pruned as stale). The caller
    no longer holds a connection slot and must not assume it does."""


def acquire_connection_slot(timeout_seconds: float = DEFAULT_ACQUIRE_TIMEOUT) -> str | None:
    """Blocks (polling, fairly-queued) until a concurrent-connection slot is
    free. Returns a unique lease_id to pass to renew_slot_lease() /
    release_connection_slot(), or None on timeout. Every acquire attempt
    (including retries across polls) uses the SAME waiter_id so this call
    keeps its place in the fairness queue rather than losing its spot on
    every retry."""
    waiter_id = str(uuid.uuid4())
    deadline = time.time() + timeout_seconds
    poll_interval = 0.5
    waited = 0.0

    while time.time() < deadline:
        now = time.time()
        got = _try_acquire(
            keys=[_ACTIVE_SLOTS_KEY, _WAITING_QUEUE_KEY],
            args=[now, MAX_CONCURRENT_CONNECTIONS,
                  LEASE_TTL_SECONDS, waiter_id, WAITER_STALE_SECONDS],
        )
        if got:
            if waited > 5:  # only log meaningful waits, not routine sub-5s ones
                logger.info(
                    "Acquired CDSE connection slot after %.1fs wait", waited)
            _redis.incrbyfloat(_STAT_WAIT_SECONDS, waited)
            return waiter_id
        time.sleep(poll_interval)
        waited += poll_interval

    _redis.incr(_STAT_ACQUIRE_TIMEOUT_COUNT)
    log_error(
        "cdse_rate_limiter",
        f"Timed out after {timeout_seconds}s waiting for a CDSE connection slot",
        level="warning",
        context={"max_concurrent": MAX_CONCURRENT_CONNECTIONS},
        include_traceback=False,  # not inside an except block -- no real traceback to attach
    )
    return None


def renew_slot_lease(lease_id: str):
    """Call periodically while still using a slot for a long operation (e.g.
    every ~20s during a streaming download) to keep the lease from expiring
    out from under you. Raises CdseLeaseLost if the lease is already gone --
    treat that as 'I no longer hold a slot', not as a warning to ignore."""
    now = time.time()
    ok = _renew_lease(keys=[_ACTIVE_SLOTS_KEY], args=[
                      now, LEASE_TTL_SECONDS, lease_id])
    if not ok:
        raise CdseLeaseLost(
            f"Lease {lease_id} was not found/already expired -- slot no longer held")


def release_connection_slot(lease_id: str):
    try:
        _release(keys=[_ACTIVE_SLOTS_KEY, _WAITING_QUEUE_KEY], args=[lease_id])
    except redis.RedisError:
        logger.exception("Failed to release CDSE lease %s (Redis error) -- "
                         "will self-heal via lease TTL expiry", lease_id)


def wait_for_rate_slot():
    """Sliding-window limiter over the last 60s, shared across all workers via
    a Redis sorted set (score = timestamp). Blocks (polling) until under the
    per-minute cap."""
    while True:
        now = time.time()
        window_start = now - 60
        pipe = _redis.pipeline()
        pipe.zremrangebyscore(_RATE_WINDOW_KEY, 0, window_start)
        pipe.zcard(_RATE_WINDOW_KEY)
        _, count = pipe.execute()

        if count < MAX_REQUESTS_PER_MINUTE:
            _redis.zadd(_RATE_WINDOW_KEY, {f"{now}:{uuid.uuid4()}": now})
            _redis.expire(_RATE_WINDOW_KEY, 120)
            return
        time.sleep(1)


def get_stats() -> dict:
    """Lightweight visibility into how hard the limiter is actually being
    hit -- call this from a debug endpoint or log it periodically. Nothing
    like this existed before; there was no way to tell whether 3/120 were
    comfortably under capacity or the real bottleneck without this."""
    return {
        "active_slots": _redis.zcard(_ACTIVE_SLOTS_KEY),
        "waiting": _redis.zcard(_WAITING_QUEUE_KEY),
        "requests_last_60s": _redis.zcard(_RATE_WINDOW_KEY),
        "total_wait_seconds": float(_redis.get(_STAT_WAIT_SECONDS) or 0),
        "total_429_count": int(_redis.get(_STAT_429_COUNT) or 0),
        "total_acquire_timeouts": int(_redis.get(_STAT_ACQUIRE_TIMEOUT_COUNT) or 0),
    }


def cdse_request(method: str, url: str, **kwargs) -> requests.Response:
    """Drop-in replacement for requests.get/post/etc against any CDSE
    endpoint (OData search, $value download, token refresh is handled
    separately by auth_broker). Applies the connection-slot limit, the
    request-rate limit, injects the auth header, and retries with
    exponential backoff (honoring Retry-After when CDSE sends one) on 429
    and 5xx responses.

    Use this for short request/response calls only. For a streaming
    download, use acquire_connection_slot()/renew_slot_lease()/
    release_connection_slot() directly -- see download_manager.py -- since
    a slot must stay held for the full transfer, not just until headers
    arrive.
    """
    headers = kwargs.pop("headers", {}) or {}
    headers.update(auth_broker.auth_header())

    for attempt in range(1, _MAX_RETRIES + 1):
        lease_id = acquire_connection_slot()
        if lease_id is None:
            raise CdseRateLimitExceeded(
                "Timed out waiting for a free CDSE connection slot -- too many "
                "concurrent requests in flight across workers")
        wait_for_rate_slot()

        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
        finally:
            release_connection_slot(lease_id)

        if resp.status_code == 429 or resp.status_code >= 500:
            if resp.status_code == 429:
                _redis.incr(_STAT_429_COUNT)
            retry_after = resp.headers.get("Retry-After")
            wait = float(
                retry_after) if retry_after else _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "CDSE returned %s on attempt %d/%d, backing off %.1fs: %s",
                resp.status_code, attempt, _MAX_RETRIES, wait, url,
            )
            if attempt == _MAX_RETRIES:
                if resp.status_code == 429:
                    log_error("cdse_rate_limiter",
                              f"CDSE rate limit exceeded after {_MAX_RETRIES} retries",
                              level="warning", context={"url": url}, include_traceback=False)
                    raise CdseRateLimitExceeded(
                        f"CDSE rate limit exceeded after {_MAX_RETRIES} retries: {url}")
                resp.raise_for_status()  # let a real 5xx surface as a normal HTTPError
            time.sleep(wait)
            continue

        return resp

    # unreachable, satisfies type checkers
    raise CdseRateLimitExceeded(f"Exhausted retries against CDSE: {url}")
