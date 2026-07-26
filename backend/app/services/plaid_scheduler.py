"""Periodic Plaid sync — what makes this a live app rather than a CSV viewer.

Why this lives in a module instead of a closure inside ``main.lifespan``:

* the schedule becomes testable on its own — no ASGI startup, no real clock;
* the manual ``POST /api/plaid/sync`` endpoint has to share the *same* lock as
  the scheduled pass, so the lock needs a home both sides can import.

Cadence and cost
----------------
``PLAID_SYNC_INTERVAL_MIN`` defaults to 60 (hourly); ``0`` disables scheduling
entirely. One pass issues one ``/transactions/sync`` request per linked item per
page of results, and an incremental page is nearly always a single empty page —
so hourly with the one linked item this install has is ~24 requests/day
(~730/month); four items would be ~96/day. ``/transactions/sync`` is
cursor-based (it returns only what changed since the stored cursor), Plaid bills
the Transactions product per connected item per month rather than per request,
and this volume is orders of magnitude below the per-item rate limit. So the
cadence is a rounding error in both cost and quota; every 15 minutes would also
have been fine, and hourly was chosen only because bank feeds themselves rarely
refresh faster than that.

Boot behaviour — the actual bug this module fixes
-------------------------------------------------
The previous loop slept a full interval BEFORE its first pass. Every container
restart reset that timer, and restarts are frequent during development, so a
scheduled sync in practice almost never fired: the owner found
``last_synced_at`` 14 hours old while a 6-hour interval was configured. Now the
first pass runs ``STARTUP_GRACE_SECONDS`` after boot and syncs straight away if
the data is already stale, so no restart cadence can starve the scheduler.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Hourly. The owner's ask, and cheap enough to be uninteresting (see above).
DEFAULT_INTERVAL_MIN = 60

# How long after boot the first pass runs. Everything synchronous in
# ``lifespan`` (migrations, category/account seeding, context inference) has
# already finished before this task is even created, so the grace period is not
# guarding those; it exists so a restart does not fire a Plaid sync while the
# process is still warming up and serving its first page loads. A long-running
# background auto-import is handled by an explicit check instead of a timer,
# because no timer is long enough for that (see ``_import_running``).
#
# 90 seconds is comfortably past the warm-up and still means "a restarted app is
# up to date within two minutes". ``PLAID_SYNC_STARTUP_GRACE_SEC`` overrides it —
# a deliberate seam so the boot pass can be smoke-tested in one second instead of
# waiting 90, and so an operator can lengthen it on a slow host.
STARTUP_GRACE_SECONDS = 90

# One shared, process-level lock. `asyncio.Lock` is the right primitive rather
# than a DB flag: there is exactly one event loop in this process, the loop and
# the endpoint both run in it, and a lock in the DB would need cleanup after a
# crash. It does NOT protect against a second *process* syncing concurrently —
# but row upserts are atomic (see plaid_sync._upsert_transaction), so the worst
# case there is wasted Plaid calls, not corruption.
_sync_lock = asyncio.Lock()


class SyncInProgress(RuntimeError):
    """Raised when a sync is asked for while one is already running."""


def sync_in_progress() -> bool:
    return _sync_lock.locked()


@asynccontextmanager
async def sync_guard():
    """Serialise full syncs; raise rather than queue if one is already running.

    Fail-fast instead of wait, because the two callers want opposite things and
    fail-fast is the only honest answer for the one that matters:

    * the manual "Sync now" button is a foreground HTTP request. Queueing it
      behind an in-flight sync would hang the request for as long as that sync
      takes — a first 730-day pull is minutes — and the user would see a
      timeout, not a result. ``409 sync already running`` is instant and true.
    * the scheduled pass has nothing to gain from waiting: whatever is running
      is pulling the same cursor, so its work is already being done. Skipping
      and retrying next interval is strictly better than piling up.

    The ``locked()`` check and ``acquire()`` are race-free here: an uncompleted
    ``Lock.acquire()`` on an uncontended lock returns without yielding to the
    event loop, so no other coroutine can slip between the two statements.
    """
    if _sync_lock.locked():
        raise SyncInProgress("A Plaid sync is already running; try again in a moment.")
    async with _sync_lock:
        yield


def interval_minutes(env: Optional[Dict[str, str]] = None) -> int:
    """Configured cadence in minutes. 0 disables. Junk values fall back to hourly."""
    raw = (env if env is not None else os.environ).get("PLAID_SYNC_INTERVAL_MIN")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_INTERVAL_MIN
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("PLAID_SYNC_INTERVAL_MIN=%r is not a number; using %d",
                       raw, DEFAULT_INTERVAL_MIN)
        return DEFAULT_INTERVAL_MIN
    return max(0, value)


def startup_grace_seconds() -> int:
    raw = os.environ.get("PLAID_SYNC_STARTUP_GRACE_SEC")
    if raw is None or str(raw).strip() == "":
        return STARTUP_GRACE_SECONDS
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return STARTUP_GRACE_SECONDS


def parse_synced_at(value: Optional[str]) -> Optional[datetime]:
    """Read a ``last_synced_at`` the way ``sync_item`` writes it.

    The column is written with SQLite's ``datetime('now')``: UTC, no timezone
    suffix. Reading it with a naive ``datetime.now()`` would be wrong by the
    host's UTC offset — on this box that is 4 hours, i.e. enough to call fresh
    data stale (or the reverse) every single boot. So it is parsed explicitly as
    UTC and compared against an aware ``now``.
    """
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.warning("Unparseable plaid_items.last_synced_at %r; treating as stale", value)
    return None


def is_stale(last_synced_at: Optional[str], interval_min: int,
             now: Optional[datetime] = None) -> bool:
    """Pure decision: is a sync overdue? The whole schedule turns on this.

    ``None`` (linked but never synced, or an unreadable timestamp) is overdue —
    the safe direction, since the cost of one extra cursor sync is a single cheap
    request and the cost of skipping is stale data.
    """
    stamp = parse_synced_at(last_synced_at)
    if stamp is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now - stamp >= timedelta(minutes=interval_min)


async def last_synced_at(db) -> Optional[str]:
    """Freshest ``last_synced_at`` across items, or None if none/never."""
    cursor = await db.execute("SELECT MAX(last_synced_at) FROM plaid_items")
    row = await cursor.fetchone()
    return row[0] if row else None


async def has_items(db) -> bool:
    cursor = await db.execute("SELECT COUNT(*) FROM plaid_items")
    row = await cursor.fetchone()
    return bool(row and row[0])


async def db_is_stale(db, interval_min: int, now: Optional[datetime] = None) -> bool:
    """``is_stale`` against the DB. Nothing linked is never stale."""
    if not await has_items(db):
        return False
    return is_stale(await last_synced_at(db), interval_min, now=now)


def _import_running() -> bool:
    from app.services.import_state import import_state

    return import_state.state == "importing"


async def run_sync_pass(*, client=None, db_factory=None, categorize: bool = True):
    """One guarded full sync. Returns the totals dict, or ``None`` if skipped.

    Skips (returning ``None``) when Plaid is unconfigured, nothing is linked, an
    import is still running, or another sync holds the lock.
    """
    from app.database import get_db
    from app.services import plaid_client, plaid_sync

    if not plaid_client.is_configured():
        return None
    if _import_running():
        logger.info("Scheduled Plaid sync skipped: an import is still running")
        return None

    db = await (db_factory or get_db)()
    try:
        if not await has_items(db):
            return None  # nothing linked yet
        try:
            async with sync_guard():
                # `categorize` stays bounded to the rows this sync added —
                # plaid_sync.sync_all passes limit=len(new_ids). An unbounded
                # pass is one LLM call per uncategorized row (see AGENTS.md).
                result = await plaid_sync.sync_all(db, client=client, categorize=categorize)
        except SyncInProgress:
            logger.info("Scheduled Plaid sync skipped: a sync is already running")
            return None
        logger.info("Scheduled Plaid sync: %s",
                    {k: v for k, v in result.items() if k != "new_ids"})
        return result
    finally:
        await db.close()


async def sync_loop(
    interval_min: int,
    *,
    client=None,
    db_factory=None,
    sleep=asyncio.sleep,
    grace_seconds: Optional[int] = None,
    max_passes: Optional[int] = None,
) -> Dict[str, Any]:
    """Sync on boot if stale, then every ``interval_min`` minutes, forever.

    Resilience rules, all of them load-bearing:

    * one failing pass must never kill the loop — every pass is wrapped;
    * it must never busy-spin — the sleep happens before any ``continue``, so
      even a pass that fails instantly waits a full interval before retrying;
    * ``asyncio.CancelledError`` is a ``BaseException``, so ``except Exception``
      below does not swallow shutdown.

    ``sleep``/``client``/``db_factory``/``grace_seconds``/``max_passes`` are all
    injectable so the schedule can be asserted on the delays it *requests*
    instead of by waiting: a test that slept an hour would not be a test. The
    returned dict is for those tests; production never sees it (``max_passes``
    is ``None``, so the loop does not return).
    """
    if grace_seconds is None:
        grace_seconds = startup_grace_seconds()
    interval_seconds = interval_min * 60
    # First tick is short: boot-grace, not a full interval. Never longer than
    # the interval itself, so a deliberately tiny interval stays tiny.
    delay = min(grace_seconds, interval_seconds)
    first_pass = True
    stats = {"passes": 0, "synced": 0, "skipped_fresh": 0, "errors": 0}

    while max_passes is None or stats["passes"] < max_passes:
        await sleep(delay)
        # Set the steady-state delay up front: every path below — success,
        # skip, or exception — then waits a full interval, and none can spin.
        delay = interval_seconds
        stats["passes"] += 1
        try:
            if first_pass:
                # Only the boot pass is conditional. On later ticks a full
                # interval has elapsed by construction, so it is due.
                from app.database import get_db

                db = await (db_factory or get_db)()
                try:
                    due = await db_is_stale(db, interval_min)
                finally:
                    await db.close()
                first_pass = False
                if not due:
                    stats["skipped_fresh"] += 1
                    logger.info("Plaid data is fresh on boot; first scheduled sync in %d min",
                                interval_min)
                    continue
                logger.info("Plaid data is stale on boot — syncing now instead of "
                            "waiting %d min", interval_min)
            result = await run_sync_pass(client=client, db_factory=db_factory)
            if result is not None:
                stats["synced"] += 1
        except Exception as exc:  # never let one bad pass end the loop
            stats["errors"] += 1
            logger.warning("Scheduled Plaid sync failed: %s", exc)

    return stats
