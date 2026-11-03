"""One leased close path for commands and the periodic closer."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from chatcore.events import ChatRef, MessageRef, PollAnswer
from chatcore.platform import PlatformError
from chatcore.testing import message, user
from chatcore_polls.closer import PollsCloser
from chatcore_polls.results import format_results


async def command(plugin, ctx, name, args=""):
    return await next(c for c in plugin.commands() if c.name == name).handler(ctx, args)


async def create(plugin, ctx, *, anonymous=False):
    await command(plugin, ctx, "poll", ("--anon " if anonymous else "") + "Dinner? | Ramen | Tacos")


@pytest.mark.parametrize(
    "counts,expected",
    [
        ((4, 2), "Winner: Ramen (4 votes)."),
        ((4, 4), "Tie: Ramen and Tacos (4 votes each)."),
        ((0, 0), "No votes."),
    ],
)
async def test_format_results_winner_tie_no_votes(setup_poll, counts, expected):
    plugin, ctx, repo, _ = setup_poll
    await create(plugin, ctx)
    poll = next(iter(repo.rows.values()))
    text = format_results(poll, counts, [])
    assert text.startswith("📊 Poll #1 closed: Dinner?\nRamen — ")
    assert expected in text
    assert "Voters" not in text


async def test_format_three_way_tie_multichoice_and_named_lists(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    await create(plugin, ctx)
    poll = replace(next(iter(repo.rows.values())), options=("Ramen", "Tacos", "Pho"), multiple=True)
    votes = [
        dict(option_index=0, voter_name="Alice", voter_key="fake:alice"),
        dict(option_index=1, voter_name="Bob", voter_key="fake:bob"),
    ]
    text = format_results(poll, (2, 2, 2), votes)
    assert "(voters could pick several)" in text
    assert "Tie: Ramen, Tacos and Pho (2 votes each)." in text
    assert "Voters — Ramen: Alice; Tacos: Bob" in text
    assert "Voters" not in format_results(replace(poll, anonymous=True), (2, 2, 2), votes)


async def test_format_truncates_long_voter_lists_with_remaining_count(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    await create(plugin, ctx)
    poll = next(iter(repo.rows.values()))
    votes = [
        dict(option_index=0, voter_name=f"Alice {i} 😀", voter_key=f"fake:{i}") for i in range(100)
    ]
    text = format_results(poll, (100, 0), votes, max_length=250)
    assert len(text.encode("utf-16-le")) // 2 <= 250
    assert "+" in text and " more" in text
    assert "Winner: Ramen (100 votes)." in text


async def test_creator_close_posts_authoritative_counts_in_same_topic_and_logs_once(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    await create(plugin, ctx)
    assert await command(plugin, ctx, "closepoll", "1") is None
    poll = next(iter(repo.rows.values()))
    assert poll.status == "closed" and poll.final_counts == (4, 2) and poll.total_voters == 6
    assert ctx.platform.stops == [MessageRef(ctx.chat, "100", "7")]
    assert len(ctx.platform.sent) == 1 and ctx.platform.sent[0].ref.thread_id == "7"
    assert "Ramen — 4" in ctx.platform.texts[0]
    await command(plugin, ctx, "closepoll", "1")
    assert len(ctx.platform.sent) == 1
    history = await ctx.stores.repo.get_message(ctx.chat, ctx.platform.sent[0].ref.message_id)
    assert history.text == ctx.platform.texts[0]


@pytest.mark.parametrize("actor", ["owner", "native", "member"])
async def test_manual_close_permissions(setup_poll, actor):
    plugin, ctx, _, _ = setup_poll
    await create(plugin, ctx)
    ctx.user = user(actor)
    if actor == "native":
        ctx.platform.admins.add((ctx.chat.key, ctx.user.ref.key))
    reply = await command(plugin, ctx, "closepoll", "1")
    assert (reply is None) == (actor != "member")
    assert len(ctx.platform.stops) == (actor != "member")


async def test_close_by_reply_and_scope_isolation(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    await create(plugin, ctx)
    assert "No open" in await command(plugin, replace(ctx, thread_id="8"), "closepoll", "1")
    plugin.access.bootstrap_chats = frozenset({ctx.chat.key, "fake:other"})
    other = replace(ctx, chat=ChatRef("fake", "other"))
    assert await command(plugin, other, "polls") == "No open polls here."
    assert "No open" in await command(plugin, other, "closepoll", "1")
    assert "#1 · Dinner? · single · named · closes manually" == await command(plugin, ctx, "polls")
    ctx.message = replace(
        message("/closepoll", thread_id="7"), reply_to=MessageRef(ctx.chat, "100", "7")
    )
    assert await command(plugin, ctx, "closepoll") is None
    assert next(iter(repo.rows.values())).status == "closed"


async def test_held_close_claim_is_refused_and_only_fenced_winner_posts(setup_poll):
    plugin, ctx, repo, now = setup_poll
    await create(plugin, ctx)
    poll = next(iter(repo.rows.values()))
    claim = (await repo.claim_close(poll.id, now[0], 1, scope=poll.scope))[0]
    assert await command(plugin, ctx, "closepoll", "1") == "That poll is already closing."
    repo.finish_wins = False
    await plugin.service.close_claim(claim, ctx.stores)
    assert not ctx.platform.sent


async def test_scheduled_closer_posts_once_and_skips_future_poll(setup_poll):
    plugin, ctx, repo, now = setup_poll
    await command(plugin, ctx, "poll", "--close 2026-11-01T12:00:00 UTC Q | A | B")
    closer = PollsCloser(plugin.service, ctx.stores)
    assert await closer.run_once() == 0
    now[0] += timedelta(days=2)
    assert await closer.run_once() == 1
    assert await closer.run_once() == 0
    assert len(ctx.platform.sent) == 1


@pytest.mark.parametrize("anonymous", [False, True])
async def test_stop_failure_retries_then_falls_back_after_limit(setup_poll, anonymous):
    plugin, ctx, repo, now = setup_poll
    await create(plugin, ctx, anonymous=anonymous)
    poll = next(iter(repo.rows.values()))
    if not anonymous:
        await plugin.on_poll_answer(PollAnswer(poll.platform_poll_id, ctx.user, (0,)), ctx.stores)
    ctx.platform.stop_errors = 3
    assert "retried" in await command(plugin, ctx, "closepoll", "1")
    closer = PollsCloser(plugin.service, ctx.stores)
    for attempt in range(2):
        now[0] += timedelta(seconds=60)
        assert await closer.run_once() == 1
        assert len(ctx.platform.sent) == (attempt == 1)
    assert repo.rows[poll.id].status == "closed" and repo.rows[poll.id].final_counts is None
    text = ctx.platform.texts[0]
    assert ("Final results are unavailable." if anonymous else "Winner: Ramen (1 vote).") in text
    if not anonymous:
        assert "Ramen — 1" in text and "Voters — Ramen: Alice" in text


async def test_refresh_failure_skips_tick_and_disallowed_poll_is_not_closed(setup_poll, caplog):
    plugin, ctx, repo, now = setup_poll
    await create(plugin, ctx)
    poll = next(iter(repo.rows.values()))
    repo.rows[poll.id] = replace(poll, close_at=now[0])
    plugin.access.refresh = AsyncMock(side_effect=RuntimeError("private error"))
    closer = PollsCloser(plugin.service, ctx.stores)
    assert await closer.run_once() == 0 and repo.claimed == 0
    assert "RuntimeError" in caplog.text and "private error" not in caplog.text
    plugin.access.refresh = AsyncMock()
    plugin.access.bootstrap_chats = frozenset()
    await closer.run_once()
    assert not ctx.platform.stops and not ctx.platform.sent


async def test_result_send_failure_does_not_retry_or_reopen(setup_poll, caplog):
    plugin, ctx, repo, _ = setup_poll
    await create(plugin, ctx)
    ctx.platform.send = AsyncMock(side_effect=PlatformError("private content"))
    await command(plugin, ctx, "closepoll", "1")
    await command(plugin, ctx, "closepoll", "1")
    ctx.platform.send.assert_awaited_once()
    assert next(iter(repo.rows.values())).status == "closed"
    assert "private content" not in caplog.text


async def test_closer_lifecycle_start_once_and_cancel(setup_poll):
    plugin, ctx, _, _ = setup_poll
    entered = asyncio.Event()
    never = asyncio.Event()

    async def sleep(_seconds):
        entered.set()
        await never.wait()

    closer = PollsCloser(plugin.service, ctx.stores, sleep=sleep)
    await closer.start()
    task = closer._task
    await closer.start()
    assert closer._task is task
    await asyncio.wait_for(entered.wait(), 1)
    await closer.aclose()
    assert task.done()


async def test_removed_group_cannot_starve_allowed_group_close_scan(setup_poll):
    from uuid import uuid4

    from chatcore_polls.models import Scope

    plugin, ctx, repo, now = setup_poll
    await create(plugin, ctx)
    allowed = next(iter(repo.rows.values()))
    repo.rows = {}
    for i in range(10):
        removed = replace(
            allowed,
            id=uuid4(),
            scope=Scope("fake:removed", False, "7"),
            number=i + 1,
            close_at=now[0] - timedelta(days=1),
        )
        repo.rows[removed.id] = removed
    repo.rows[allowed.id] = replace(allowed, close_at=now[0])
    closer = PollsCloser(plugin.service, ctx.stores)
    for _ in range(6):
        await closer.run_once()
        now[0] += timedelta(seconds=30)
    assert repo.rows[allowed.id].status == "closed"
    assert len(ctx.platform.stops) == 1
    assert len(ctx.platform.sent) == 1


async def test_maximum_unicode_results_preserve_counts_tie_and_voter_overflow(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    await create(plugin, ctx)
    poll = replace(
        next(iter(repo.rows.values())),
        question="😀" * 300,
        options=tuple(chr(65 + i) + "😀" * 99 for i in range(12)),
    )
    votes = [dict(option_index=i, voter_name="😀" * 100, voter_key=f"fake:{i}") for i in range(12)]
    text = format_results(poll, (1,) * 12, votes)
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert text.count(" — 1") == 12
    assert "Tie:" in text and "(1 vote each)." in text
    assert "+1 more" in text


@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_each_close_tick_sweeps_stale_sending_rows_globally(
    setup_poll, caplog, refresh_fails
):
    from uuid import uuid4

    from chatcore_polls.models import Scope

    plugin, ctx, repo, now = setup_poll
    await create(plugin, ctx)
    opened = next(iter(repo.rows.values()))
    stale = replace(
        opened,
        id=uuid4(),
        scope=Scope("fake:removed", False, "8"),
        status="sending",
        created_at=now[0] - timedelta(seconds=601),
    )
    fresh = replace(opened, id=uuid4(), status="sending", created_at=now[0])
    repo.rows.update({stale.id: stale, fresh.id: fresh})
    if refresh_fails:
        plugin.access.refresh = AsyncMock(side_effect=RuntimeError("private snapshot error"))
    closer = PollsCloser(plugin.service, ctx.stores)
    with caplog.at_level("INFO", logger="chatcore_polls.closer"):
        assert await closer.run_once() == 0
    assert repo.rows[stale.id].status == "failed"
    assert repo.rows[stale.id].closed_at == now[0]
    assert repo.rows[fresh.id] == fresh and repo.rows[opened.id] == opened
    assert "stale poll reservations failed count=1" in caplog.messages
    assert all("Dinner?" not in text and "fake:removed" not in text for text in caplog.messages)
    assert not ctx.platform.stops and not ctx.platform.sent


async def test_bare_closepoll_lists_open_polls_and_asks_for_a_number(setup_poll):
    plugin, ctx, _repo, _ = setup_poll
    assert await command(plugin, ctx, "closepoll") == "No open polls here."
    await create(plugin, ctx)
    asked = await command(plugin, ctx, "closepoll")
    assert asked.awaits_reply and "Dinner?" in asked.text
    assert "Reply to this message with the number of the poll to close." in asked.text
    bad = await command(plugin, ctx, "closepoll", "first")
    assert bad.awaits_reply and "poll number" in bad.text
    assert await command(plugin, ctx, "closepoll", "#1") is None


async def test_bare_polltools_asks_for_on_or_off(setup_poll):
    plugin, ctx, _repo, _ = setup_poll
    asked = await command(plugin, ctx, "polltools")
    assert asked.awaits_reply and "on or off" in asked.text
