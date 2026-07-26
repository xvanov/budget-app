"""The Plaid *schedule* — no network, no LLM, no real clock.

These are regression tests for a live-data bug, not for a timer: the loop used to
sleep a full interval before its first pass, so every restart reset the timer and
a scheduled sync in practice never fired (``last_synced_at`` was found 14 hours
old with a 6-hour interval configured).

`sleep` is injected so the schedule is asserted on the *durations it asks for*
rather than by waiting; a test that waited an hour would not be a test.
"""
import asyncio
import base64
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from app.services import plaid_scheduler, plaid_sync

TEST_KEY = base64.b64encode(b"budget-app-test-key-not-a-secret").decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_KEY)
    # Plaid credentials only need to look configured; no request ever leaves.
    monkeypatch.setenv("PLAID_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "test-secret")


@pytest_asyncio.fixture
async def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", path)
    import app.database as database

    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    return path


@pytest_asyncio.fixture
async def db(db_path):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    yield conn
    await conn.close()


@pytest.fixture
def db_factory(db_path):
    """Hands the scheduler a fresh connection per pass, like get_db does."""

    async def _factory():
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        return conn

    return _factory


class StubClient:
    """Counts transactions_sync calls so 'did it actually sync' is checkable."""

    def __init__(self, pages=None):
        self.calls = 0
        self.pages = pages

    def transactions_sync(self, access_token, cursor=None, count=500):
        self.calls += 1
        if self.pages:
            return self.pages.pop(0)
        return {"added": [], "modified": [], "removed": [],
                "next_cursor": f"cur-{self.calls}", "has_more": False}


async def _seed_item(db, last_synced_at=None):
    item_db_id = await plaid_sync.store_item(
        db, item_id="item-abc", access_token="access-sandbox-token",
        institution_id="ins_56", institution_name="Chase",
    )
    await db.execute(
        """INSERT INTO plaid_accounts (item_db_id, plaid_account_id, name, mask,
               canonical_account_id, include_in_sync) VALUES (?,?,?,?,?,?)""",
        (item_db_id, "plaid-acct-1", "Amazon Prime Visa", "0002", "card-0002", 1),
    )
    if last_synced_at is not None:
        await db.execute("UPDATE plaid_items SET last_synced_at=? WHERE id=?",
                         (last_synced_at, item_db_id))
    await db.commit()
    return item_db_id


class RecordingSleep:
    """Records requested sleeps; ends the loop by raising after N of them."""

    def __init__(self, stop_after=None):
        self.calls = []
        self.stop_after = stop_after

    async def __call__(self, seconds):
        self.calls.append(seconds)
        if self.stop_after is not None and len(self.calls) >= self.stop_after:
            raise asyncio.CancelledError()
        # Yield to the loop, but do not actually wait.
        await asyncio.sleep(0)


# ------------------------------------------------------------------- cadence

def test_default_interval_is_hourly(monkeypatch):
    """The old default was 360 minutes. The owner asked for roughly hourly."""
    monkeypatch.delenv("PLAID_SYNC_INTERVAL_MIN", raising=False)
    assert plaid_scheduler.interval_minutes() == 60
    assert plaid_scheduler.DEFAULT_INTERVAL_MIN == 60


def test_compose_default_is_hourly_too():
    """docker-compose supplies the env var, so its default is the real default."""
    from pathlib import Path

    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text()
    assert "PLAID_SYNC_INTERVAL_MIN=${PLAID_SYNC_INTERVAL_MIN:-60}" in text


def test_zero_still_disables_scheduling(monkeypatch):
    monkeypatch.setenv("PLAID_SYNC_INTERVAL_MIN", "0")
    assert plaid_scheduler.interval_minutes() == 0


@pytest.mark.parametrize("raw,expected", [("15", 15), ("", 60), ("nonsense", 60), ("-5", 0)])
def test_interval_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("PLAID_SYNC_INTERVAL_MIN", raw)
    assert plaid_scheduler.interval_minutes() == expected


# ------------------------------------------- staleness (pure, no clock needed)

NOW = datetime(2026, 7, 25, 23, 26, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("last_synced_at,interval,expected", [
    # The owner's actual situation: 14 hours old with an hourly cadence.
    ("2026-07-25 09:26:46", 60, True),
    ("2026-07-25 22:26:00", 60, True),    # exactly one interval old -> due
    ("2026-07-25 22:56:00", 60, False),   # half an interval -> not yet
    ("2026-07-25 23:26:00", 60, False),   # just synced
    ("2026-07-25 17:26:00", 360, True),   # 6h old on the OLD 6h cadence
    (None, 60, True),                     # linked but never synced
    ("", 60, True),
    ("not a timestamp", 60, True),        # unreadable -> sync, don't guess
    ("2026-07-25 22:25:00.123456", 60, True),   # SQLite may include millis
    ("2026-07-25T22:26:00", 60, True),          # ...or an ISO 'T'
])
def test_is_stale_is_a_pure_decision(last_synced_at, interval, expected):
    assert plaid_scheduler.is_stale(last_synced_at, interval, now=NOW) is expected


def test_timestamps_are_read_as_utc_not_local():
    """last_synced_at is SQLite datetime('now') — UTC with no suffix.

    Reading it as local time would be off by the host's offset (4h here), which
    is enough to call fresh data stale, or stale data fresh, on every boot.
    """
    stamp = plaid_scheduler.parse_synced_at("2026-07-25 09:26:46")
    assert stamp == datetime(2026, 7, 25, 9, 26, 46, tzinfo=timezone.utc)
    # A sync 30 minutes ago in UTC terms is NOT due on an hourly cadence, no
    # matter what the host's timezone is.
    thirty_min_ago = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    assert plaid_scheduler.is_stale(thirty_min_ago, 60, now=NOW) is False


@pytest.mark.asyncio
async def test_no_linked_items_is_never_stale(db):
    assert await plaid_scheduler.db_is_stale(db, 60) is False


@pytest.mark.asyncio
async def test_linked_but_never_synced_is_stale(db):
    await _seed_item(db)
    assert await plaid_scheduler.last_synced_at(db) is None
    assert await plaid_scheduler.db_is_stale(db, 60) is True


@pytest.mark.asyncio
async def test_db_staleness_reads_the_freshest_item(db):
    await _seed_item(db, last_synced_at="2026-07-25 09:26:46")
    assert await plaid_scheduler.db_is_stale(db, 60, now=NOW) is True
    await db.execute("UPDATE plaid_items SET last_synced_at='2026-07-25 23:20:00'")
    await db.commit()
    assert await plaid_scheduler.db_is_stale(db, 60, now=NOW) is False


# ------------------------------------------------------------- grace-period seam

def test_grace_period_default_and_override(monkeypatch):
    monkeypatch.delenv("PLAID_SYNC_STARTUP_GRACE_SEC", raising=False)
    assert plaid_scheduler.startup_grace_seconds() == 90
    monkeypatch.setenv("PLAID_SYNC_STARTUP_GRACE_SEC", "1")
    assert plaid_scheduler.startup_grace_seconds() == 1
    monkeypatch.setenv("PLAID_SYNC_STARTUP_GRACE_SEC", "junk")
    assert plaid_scheduler.startup_grace_seconds() == 90


# ------------------------------------------------------------- boot behaviour

@pytest.mark.asyncio
async def test_stale_on_boot_syncs_after_the_grace_period_not_a_full_interval(db, db_factory):
    """THE regression. Old loop: sleep(interval) first, so a restart starved it.

    A restart with 14-hour-old data must sync within the boot grace period.
    """
    await _seed_item(db)
    await db.execute("UPDATE plaid_items SET last_synced_at=datetime('now','-14 hours')")
    await db.commit()

    client = StubClient()
    sleep = RecordingSleep(stop_after=2)
    with pytest.raises(asyncio.CancelledError):
        await plaid_scheduler.sync_loop(
            60, client=client, db_factory=db_factory, sleep=sleep, grace_seconds=90,
        )

    assert sleep.calls[0] == 90, "first wait must be the boot grace, not the interval"
    assert sleep.calls[1] == 3600, "then settle onto the interval"
    assert client.calls == 1, "the boot pass must actually hit Plaid"

    cursor = await db.execute("SELECT last_synced_at FROM plaid_items")
    fresh = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "SELECT (julianday('now') - julianday(?)) * 1440.0", (fresh,))
    assert (await cursor.fetchone())[0] < 1, "last_synced_at must now be ~now"


@pytest.mark.asyncio
async def test_fresh_on_boot_waits_instead_of_resyncing(db, db_factory):
    """A restart 5 minutes after a sync must not burn a redundant Plaid call."""
    await _seed_item(db)
    await db.execute("UPDATE plaid_items SET last_synced_at=datetime('now','-5 minutes')")
    await db.commit()

    client = StubClient()
    sleep = RecordingSleep(stop_after=2)
    with pytest.raises(asyncio.CancelledError):
        await plaid_scheduler.sync_loop(
            60, client=client, db_factory=db_factory, sleep=sleep, grace_seconds=90,
        )
    assert client.calls == 0
    assert sleep.calls == [90, 3600]


@pytest.mark.asyncio
async def test_grace_period_never_exceeds_the_interval(db, db_factory):
    await _seed_item(db)
    sleep = RecordingSleep(stop_after=1)
    with pytest.raises(asyncio.CancelledError):
        await plaid_scheduler.sync_loop(
            1, client=StubClient(), db_factory=db_factory, sleep=sleep, grace_seconds=90,
        )
    assert sleep.calls == [60], "a 1-minute interval must not wait 90s to start"


@pytest.mark.asyncio
async def test_loop_takes_its_grace_from_the_env_seam(db, db_factory, monkeypatch):
    """The seam that lets the boot pass be smoke-tested in a second, not 90."""
    monkeypatch.setenv("PLAID_SYNC_STARTUP_GRACE_SEC", "1")
    await _seed_item(db)
    sleep = RecordingSleep(stop_after=1)
    # grace_seconds is not passed, so the loop must resolve it from the env.
    with pytest.raises(asyncio.CancelledError):
        await plaid_scheduler.sync_loop(
            60, client=StubClient(), db_factory=db_factory, sleep=sleep)
    assert sleep.calls == [1]


@pytest.mark.asyncio
async def test_steady_state_passes_sync_every_interval(db, db_factory):
    await _seed_item(db)
    await db.execute("UPDATE plaid_items SET last_synced_at=datetime('now')")
    await db.commit()

    client = StubClient()
    stats = await plaid_scheduler.sync_loop(
        60, client=client, db_factory=db_factory, sleep=RecordingSleep(),
        grace_seconds=90, max_passes=3,
    )
    # Pass 1 skips (fresh); passes 2 and 3 are due by construction.
    assert stats == {"passes": 3, "synced": 2, "skipped_fresh": 1, "errors": 0}
    assert client.calls == 2


@pytest.mark.asyncio
async def test_nothing_linked_means_no_plaid_calls_at_all(db, db_factory):
    client = StubClient()
    stats = await plaid_scheduler.sync_loop(
        60, client=client, db_factory=db_factory, sleep=RecordingSleep(),
        grace_seconds=90, max_passes=3,
    )
    assert client.calls == 0
    assert stats["synced"] == 0


# ---------------------------------------------------------------- resilience

@pytest.mark.asyncio
async def test_a_failing_pass_does_not_kill_the_loop_or_busy_spin(db, db_factory):
    await _seed_item(db)

    class Exploding:
        calls = 0

        def transactions_sync(self, *a, **kw):
            Exploding.calls += 1
            raise RuntimeError("plaid is down")

    sleep = RecordingSleep()
    stats = await plaid_scheduler.sync_loop(
        60, client=Exploding(), db_factory=db_factory, sleep=sleep,
        grace_seconds=90, max_passes=3,
    )
    assert stats["passes"] == 3, "the loop must survive failing passes"
    assert Exploding.calls == 3
    # sync_all swallows per-item errors, so these count as completed passes.
    assert sleep.calls == [90, 3600, 3600], "a failed pass must still wait a full interval"


@pytest.mark.asyncio
async def test_an_exception_inside_the_pass_is_contained(db_factory, monkeypatch):
    """Even a bug in our own code (not Plaid's) must not end the loop."""
    async def boom(**kwargs):
        raise RuntimeError("bug in the pass")

    monkeypatch.setattr(plaid_scheduler, "run_sync_pass", boom)
    sleep = RecordingSleep()
    stats = await plaid_scheduler.sync_loop(
        60, db_factory=db_factory, sleep=sleep, grace_seconds=90, max_passes=2,
    )
    assert stats["errors"] >= 1
    assert stats["passes"] == 2
    assert sleep.calls == [90, 3600]


@pytest.mark.asyncio
async def test_cancellation_propagates_for_clean_shutdown(db, db_factory):
    """lifespan cancels the task; CancelledError must not be swallowed."""
    await _seed_item(db)
    task = asyncio.create_task(plaid_scheduler.sync_loop(
        60, client=StubClient(), db_factory=db_factory, grace_seconds=3600,
    ))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_a_running_import_defers_the_sync(db, db_factory):
    """Never sync on top of a multi-minute auto-import."""
    from app.services.import_state import import_state

    await _seed_item(db)
    client = StubClient()
    import_state.state = "importing"
    try:
        assert await plaid_scheduler.run_sync_pass(client=client, db_factory=db_factory) is None
        assert client.calls == 0
    finally:
        import_state.state = "idle"
    assert await plaid_scheduler.run_sync_pass(client=client, db_factory=db_factory) is not None
    assert client.calls == 1


# ------------------------------------------------------------------- the lock

@pytest.mark.asyncio
async def test_second_caller_is_refused_while_a_sync_runs():
    assert plaid_scheduler.sync_in_progress() is False
    async with plaid_scheduler.sync_guard():
        assert plaid_scheduler.sync_in_progress() is True
        with pytest.raises(plaid_scheduler.SyncInProgress):
            async with plaid_scheduler.sync_guard():
                pytest.fail("two syncs must never run at once")
    assert plaid_scheduler.sync_in_progress() is False


@pytest.mark.asyncio
async def test_the_lock_is_released_even_when_the_sync_raises():
    with pytest.raises(RuntimeError):
        async with plaid_scheduler.sync_guard():
            raise RuntimeError("sync blew up")
    assert plaid_scheduler.sync_in_progress() is False, "a crash must not wedge the lock"


@pytest.mark.asyncio
async def test_scheduled_pass_skips_when_a_manual_sync_holds_the_lock(db, db_factory):
    """The overlap that wastes Plaid calls and interleaves cursor writes."""
    await _seed_item(db)
    client = StubClient()
    async with plaid_scheduler.sync_guard():          # stand in for "Sync now"
        result = await plaid_scheduler.run_sync_pass(client=client, db_factory=db_factory)
    assert result is None, "the scheduled pass must stand down, not queue up"
    assert client.calls == 0, "and must not spend a Plaid call"


@pytest.mark.asyncio
async def test_manual_and_scheduled_sync_do_not_double_run(db, db_factory, monkeypatch):
    """Both callers fire at once: exactly one full sync happens.

    Drives the real endpoint function, so the endpoint and the loop are proven to
    share one lock rather than each holding their own.
    """
    from app.routers import plaid_routes

    await _seed_item(db)
    client = StubClient()
    in_flight = {"max": 0, "now": 0}
    real_sync_all = plaid_sync.sync_all

    async def counting_sync_all(conn, **kwargs):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        try:
            await asyncio.sleep(0.05)  # hold the lock long enough to overlap
            kwargs.setdefault("client", client)
            return await real_sync_all(conn, **kwargs)
        finally:
            in_flight["now"] -= 1

    # One module object, imported by both call sites — patching it once is the
    # point: if they did not share it, the overlap assertion below would fail.
    assert plaid_routes.plaid_sync is plaid_sync
    monkeypatch.setattr(plaid_sync, "sync_all", counting_sync_all)

    async def manual():
        try:
            return await plaid_routes.sync(categorize=False)
        except Exception as exc:  # HTTPException(409) is the acceptable outcome
            return exc

    scheduled = asyncio.create_task(
        plaid_scheduler.run_sync_pass(client=client, db_factory=db_factory, categorize=False))
    manual_task = asyncio.create_task(manual())
    outcomes = await asyncio.gather(scheduled, manual_task, return_exceptions=True)

    assert in_flight["max"] == 1, "two full syncs must never overlap"
    # One did the work; the other stood down (None) or was told 409.
    from fastapi import HTTPException

    did_work = [o for o in outcomes if isinstance(o, dict)]
    stood_down = [o for o in outcomes
                  if o is None or (isinstance(o, HTTPException) and o.status_code == 409)]
    assert len(did_work) == 1, f"exactly one sync should run, got {outcomes}"
    assert len(stood_down) == 1, f"the other must stand down, got {outcomes}"
    assert client.calls == 1, "the loser must not spend a Plaid call"


@pytest.mark.asyncio
async def test_manual_sync_returns_409_while_one_is_running(db, monkeypatch):
    from fastapi import HTTPException

    from app.routers import plaid_routes

    await _seed_item(db)

    async def must_not_run(*a, **kw):
        raise AssertionError("the endpoint must refuse before it reaches Plaid")

    monkeypatch.setattr(plaid_sync, "sync_all", must_not_run)
    monkeypatch.setattr(plaid_sync, "sync_item", must_not_run)

    async with plaid_scheduler.sync_guard():
        with pytest.raises(HTTPException) as excinfo:
            await plaid_routes.sync(categorize=False)
    assert excinfo.value.status_code == 409
    assert "already running" in str(excinfo.value.detail)


# --------------------------------------------------------------- status shape

@pytest.mark.asyncio
async def test_status_reports_the_cadence_so_the_ui_can_judge_staleness(db, monkeypatch):
    monkeypatch.setenv("PLAID_SYNC_INTERVAL_MIN", "60")
    from app.routers import plaid_routes

    await _seed_item(db)
    status = await plaid_routes.plaid_status()
    assert status["sync_interval_min"] == 60
    assert status["auto_sync_enabled"] is True
    assert status["sync_running"] is False

    monkeypatch.setenv("PLAID_SYNC_INTERVAL_MIN", "0")
    status = await plaid_routes.plaid_status()
    assert status["auto_sync_enabled"] is False
