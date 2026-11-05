"""Disposable Postgres migration/CAS checks without real Drive or chat access."""

import asyncio
import os
from importlib import resources
from uuid import uuid4

import asyncpg
import pytest
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore_google.repository import GoogleRepository

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")
SHEET = "application/vnd.google-apps.spreadsheet"


@pytest.fixture
async def trip_file_db():
    try:
        admin = await asyncpg.connect(DSN, timeout=2)
    except (OSError, TimeoutError, asyncpg.PostgresError):
        pytest.skip("Local development Postgres is unavailable")
    schema = "trip_files_" + uuid4().hex[:12]
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:
        pool = await asyncpg.create_pool(
            DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        yield PgDatabase(pool)
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def migrate(db):
    await migrations.apply(db, [("google", resources.files("chatcore_google.migrations"))])


def data(file_id, *, role="reference", mime="application/pdf", requester="fake:alice"):
    return {
        "url": f"https://drive.google.com/file/d/{file_id}/view",
        "name": file_id,
        "mime": mime,
        "role": role,
        "owner": "owner-id",
        "requester": requester,
    }


async def put(repo, file_id, *, chat="fake:a", trip="trip-a", **kwargs):
    await repo.apply_file(
        chat,
        trip,
        file_id,
        data(file_id, **kwargs),
        repo.file_state(await repo.trip_files(chat, trip)),
    )


async def test_additive_migration_backfills_legacy_link_without_touching_existing_trip_data(
    trip_file_db, tmp_path
):
    db = trip_file_db
    old_migrations = tmp_path / "old"
    old_migrations.mkdir()
    sql = resources.files("chatcore_google.migrations").joinpath("0001_google.sql").read_text()
    (old_migrations / "0001_google.sql").write_text(sql)
    await migrations.apply(db, [("google", old_migrations)])
    await db.execute(
        "INSERT INTO google_itinerary_links(chat_key,file_id,trip_id,requester_key) "
        "VALUES('fake:a','sheet','existing-trip','fake:alice')"
    )
    await db.execute("CREATE TABLE trips(id text PRIMARY KEY, name text, status text)")
    await db.execute("CREATE TABLE expenses(id text PRIMARY KEY, trip_id text, amount integer)")
    await db.execute("INSERT INTO trips VALUES('existing-trip','Sample & Example','planned')")
    await db.execute("INSERT INTO expenses VALUES('flight','existing-trip',12345)")
    trips_before = await db.fetch("SELECT * FROM trips")
    expenses_before = await db.fetch("SELECT * FROM expenses")

    await migrate(db)

    repo = GoogleRepository(db)
    row = await repo.trip_file("fake:a", "sheet")
    assert row["trip_id"] == "existing-trip"
    assert row["role"] == "reference" and row["owner"] is None
    assert row["version"] == "legacy" and row["requester"] == "fake:alice"
    assert await repo.linked_trip("fake:a", "sheet") == "existing-trip"
    assert await db.fetch("SELECT * FROM trips") == trips_before
    assert await db.fetch("SELECT * FROM expenses") == expenses_before
    assert (
        await migrations.apply(db, [("google", resources.files("chatcore_google.migrations"))])
        == []
    )


async def test_primary_replacement_and_legacy_sheet_link_survive_repository_restart(trip_file_db):
    await migrate(trip_file_db)
    repo = GoogleRepository(trip_file_db)
    await put(repo, "sheet", role="itinerary", mime=SHEET)
    await put(repo, "hotel")
    await put(repo, "document", role="itinerary")
    restarted = GoogleRepository(trip_file_db)
    rows = await restarted.trip_files("fake:a", "trip-a")
    assert {row["file_id"]: row["role"] for row in rows} == {
        "document": "itinerary",
        "hotel": "reference",
        "sheet": "reference",
    }
    assert await restarted.linked_trip("fake:a", "sheet") == "trip-a"


async def test_concurrent_cas_has_one_winner_and_stale_removal_does_not_change_rows(trip_file_db):
    await migrate(trip_file_db)
    repo = GoogleRepository(trip_file_db)
    await put(repo, "original", role="itinerary")
    expected = repo.file_state(await repo.trip_files("fake:a", "trip-a"))
    results = await asyncio.gather(
        *[
            repo.apply_file("fake:a", "trip-a", fid, data(fid, role="itinerary"), expected)
            for fid in ("first", "second")
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    rows = await repo.trip_files("fake:a", "trip-a")
    assert len(rows) == 2 and sum(row["role"] == "itinerary" for row in rows) == 1
    with pytest.raises(ValueError, match="changed"):
        await repo.apply_file("fake:a", "trip-a", "original", None, expected)
    assert await repo.trip_files("fake:a", "trip-a") == rows


async def test_make_current_cas_preserves_a_concurrent_primary_selection(trip_file_db):
    await migrate(trip_file_db)
    repo = GoogleRepository(trip_file_db)
    await put(repo, "original", role="itinerary", mime="application/vnd.google-apps.document")
    preview_rows = await repo.trip_files("fake:a", "trip-a")
    frozen = repo.file_state(preview_rows)
    # A separate confirmed selection wins while the Google edit is running.
    await put(repo, "newer-choice", role="itinerary", mime="application/vnd.google-apps.document")
    with pytest.raises(ValueError, match="changed"):
        await repo.apply_file(
            "fake:a",
            "trip-a",
            "verified-copy",
            data(
                "verified-copy",
                role="itinerary",
                mime="application/vnd.google-apps.document",
            ),
            frozen,
        )
    rows = await repo.trip_files("fake:a", "trip-a")
    assert {row["file_id"]: row["role"] for row in rows} == {
        "original": "reference",
        "newer-choice": "itinerary",
    }
    assert await repo.trip_file("fake:a", "verified-copy") is None


async def test_legacy_link_insert_failure_rolls_back_primary_demotion_and_new_association(
    trip_file_db,
):
    db = trip_file_db
    await migrate(db)
    repo = GoogleRepository(db)
    await put(repo, "original", role="itinerary")
    before = await repo.trip_files("fake:a", "trip-a")
    await db.execute(
        "CREATE FUNCTION reject_legacy_link() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'synthetic compatibility failure'; END $$"
    )
    await db.execute(
        "CREATE TRIGGER reject_link BEFORE INSERT ON google_itinerary_links "
        "FOR EACH ROW EXECUTE FUNCTION reject_legacy_link()"
    )
    with pytest.raises(asyncpg.RaiseError, match="synthetic compatibility failure"):
        await put(repo, "new-sheet", role="itinerary", mime=SHEET)
    assert await repo.trip_files("fake:a", "trip-a") == before
    assert await repo.trip_file("fake:a", "new-sheet") is None
    assert await repo.linked_trip("fake:a", "new-sheet") is None


async def test_other_chat_and_other_trip_cannot_remove_or_move_existing_association(trip_file_db):
    await migrate(trip_file_db)
    repo = GoogleRepository(trip_file_db)
    await put(repo, "file", role="itinerary")
    before = await repo.trip_files("fake:a", "trip-a")
    assert await repo.trip_file("fake:b", "file") is None
    for payload in (None, data("file", role="itinerary")):
        with pytest.raises(ValueError, match="another trip"):
            await repo.apply_file("fake:a", "trip-b", "file", payload, repo.file_state([]))
    with pytest.raises(ValueError, match="no longer attached"):
        await repo.apply_file("fake:b", "trip-a", "file", None, repo.file_state([]))
    assert await repo.trip_files("fake:a", "trip-a") == before
    await put(repo, "file", chat="fake:b", trip="trip-b")
    assert (await repo.trip_file("fake:b", "file"))["trip_id"] == "trip-b"


async def test_remove_and_privacy_purge_revoke_saved_and_legacy_link_authority(trip_file_db):
    await migrate(trip_file_db)
    repo = GoogleRepository(trip_file_db)
    await repo.link("fake:a", "legacy", "trip-a", "fake:alice")
    assert (await repo.trip_file("fake:a", "legacy"))["owner"] is None
    expected = repo.file_state(await repo.trip_files("fake:a", "trip-a"))
    await repo.apply_file("fake:a", "trip-a", "legacy", None, expected)
    assert await repo.trip_file("fake:a", "legacy") is None
    assert await repo.linked_trip("fake:a", "legacy") is None
    await put(repo, "alice", mime=SHEET)
    await put(repo, "bob", mime=SHEET, requester="fake:bob")
    await put(repo, "alice", chat="fake:b", mime=SHEET)
    await repo.purge("fake:a", "fake:alice")
    assert await repo.trip_file("fake:a", "alice") is None
    assert await repo.linked_trip("fake:a", "alice") is None
    assert await repo.trip_file("fake:a", "bob") is not None
    assert await repo.trip_file("fake:b", "alice") is not None
    await repo.purge("fake:a")
    assert await repo.trip_files("fake:a", "trip-a") == []
    assert await repo.linked_trip("fake:a", "bob") is None
    assert await repo.trip_file("fake:b", "alice") is not None
