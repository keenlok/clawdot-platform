"""Memory association invariants shared by all Drive formats."""

import asyncio

import pytest
from chatcore_google.repository import GoogleRepository

SHEET = "application/vnd.google-apps.spreadsheet"


def file_data(file_id, *, role="reference", mime="application/pdf", requester="fake:alice"):
    return {
        "url": f"https://drive.google.com/file/d/{file_id}/view",
        "name": file_id,
        "mime": mime,
        "role": role,
        "owner": "owner-id",
        "requester": requester,
    }


async def attach(repo, file_id, *, chat="fake:a", trip="trip-a", **kwargs):
    await repo.apply_file(
        chat,
        trip,
        file_id,
        file_data(file_id, **kwargs),
        repo.file_state(await repo.trip_files(chat, trip)),
    )


async def test_replacing_primary_demotes_previous_without_removing_reference_or_legacy_link():
    repo = GoogleRepository()
    await attach(repo, "sheet", role="itinerary", mime=SHEET)
    await attach(repo, "hotel")
    await attach(repo, "doc", role="itinerary")
    rows = await repo.trip_files("fake:a", "trip-a")
    assert {row["file_id"]: row["role"] for row in rows} == {
        "doc": "itinerary",
        "hotel": "reference",
        "sheet": "reference",
    }
    assert await repo.linked_trip("fake:a", "sheet") == "trip-a"


async def test_stale_state_cannot_replace_or_remove_primary_and_concurrent_change_has_one_winner():
    repo = GoogleRepository()
    await attach(repo, "original", role="itinerary")
    original = await repo.trip_files("fake:a", "trip-a")
    expected = repo.file_state(original)
    results = await asyncio.gather(
        *[
            repo.apply_file("fake:a", "trip-a", fid, file_data(fid, role="itinerary"), expected)
            for fid in ("first", "second")
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    rows = await repo.trip_files("fake:a", "trip-a")
    assert len(rows) == 2
    assert sum(row["role"] == "itinerary" for row in rows) == 1
    with pytest.raises(ValueError, match="changed"):
        await repo.apply_file("fake:a", "trip-a", "original", None, expected)
    assert await repo.trip_files("fake:a", "trip-a") == rows


async def test_association_is_chat_scoped_and_cannot_be_moved_or_removed_through_another_trip():
    repo = GoogleRepository()
    await attach(repo, "shared", role="itinerary")
    assert await repo.trip_file("fake:b", "shared") is None
    assert await repo.trip_files("fake:a", "other-trip") == []
    before = await repo.trip_files("fake:a", "trip-a")
    for data in (None, file_data("shared", role="itinerary")):
        with pytest.raises(ValueError, match="another trip"):
            await repo.apply_file("fake:a", "other-trip", "shared", data, repo.file_state([]))
    with pytest.raises(ValueError, match="no longer attached"):
        await repo.apply_file("fake:b", "trip-a", "shared", None, repo.file_state([]))
    await attach(repo, "shared", chat="fake:b", trip="trip-b")
    assert await repo.trip_files("fake:a", "trip-a") == before
    assert (await repo.trip_file("fake:b", "shared"))["trip_id"] == "trip-b"


async def test_legacy_sheet_link_and_remove_keep_both_representations_synchronized():
    repo = GoogleRepository()
    await repo.link("fake:a", "sheet", "trip-a", "fake:alice")
    row = await repo.trip_file("fake:a", "sheet")
    assert row["role"] == "reference" and row["owner"] is None
    await attach(repo, "sheet", role="itinerary", mime=SHEET)
    assert await repo.linked_trip("fake:a", "sheet") == "trip-a"
    state = repo.file_state(await repo.trip_files("fake:a", "trip-a"))
    await repo.apply_file("fake:a", "trip-a", "sheet", None, state)
    assert await repo.trip_file("fake:a", "sheet") is None
    assert await repo.linked_trip("fake:a", "sheet") is None


async def test_purge_removes_only_selected_chat_requesters_file_authority_and_compatibility_links():
    repo = GoogleRepository()
    await attach(repo, "alice", mime=SHEET)
    await attach(repo, "bob", mime=SHEET, requester="fake:bob")
    await attach(repo, "alice", chat="fake:b", mime=SHEET)
    await repo.purge("fake:a", "fake:alice")
    assert await repo.trip_file("fake:a", "alice") is None
    assert await repo.linked_trip("fake:a", "alice") is None
    assert await repo.trip_file("fake:a", "bob") is not None
    assert await repo.trip_file("fake:b", "alice") is not None
    await repo.purge("fake:a")
    assert await repo.trip_files("fake:a", "trip-a") == []
    assert await repo.linked_trip("fake:a", "bob") is None
    assert await repo.trip_file("fake:b", "alice") is not None
