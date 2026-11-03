"""Recording native platform and stateful repository for service tests."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from chatcore.access import AccessPolicy
from chatcore.display_time_store import DisplayTimeService, MemoryDisplayTimeStore
from chatcore.events import MessageRef
from chatcore.platform import PlatformError
from chatcore.plugin import CommandContext
from chatcore.polls import PollLimits, PollResult, SentPoll
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore_polls.models import Poll
from chatcore_polls.repository import PollConflict


class FakePollPlatform(FakePlatform):
    poll_limits = PollLimits(12, 300, 100)

    def __init__(self):
        super().__init__()
        self.polls = []
        self.stops = []
        self.admins = set()
        self.admin_error = False
        self.send_error = False
        self.stop_errors = 0
        self.result = PollResult((4, 2), 6)

    async def send_poll(self, chat, question, options, *, multiple, anonymous, thread_id=None):
        if self.send_error:
            raise PlatformError("private platform error")
        result = SentPoll(
            MessageRef(chat, str(len(self.polls) + 100), thread_id), f"native-{len(self.polls)}"
        )
        self.polls.append((question, options, multiple, anonymous, result))
        return result

    async def stop_poll(self, message):
        self.stops.append(message)
        if self.stop_errors:
            self.stop_errors -= 1
            raise PlatformError("private platform error")
        return self.result

    async def is_group_admin(self, chat, actor):
        if self.admin_error:
            raise PlatformError("private admin error")
        return (chat.key, actor.key) in self.admins


class FakeRepo:
    def __init__(self, now):
        self.now = now
        self.rows = {}
        self.config = {"polls_enabled": True, "revision": 1}
        self.ballots = []
        self.claimed = 0
        self.finish_wins = True

    async def settings(self, scope):
        return dict(self.config)

    async def configure(self, scope, enabled, expected_revision):
        if expected_revision != self.config["revision"]:
            raise PollConflict("Poll settings changed.")
        self.config = {"polls_enabled": enabled, "revision": expected_revision + 1}
        return dict(self.config)

    async def reserve(self, scope, creator, draft, source_action_id, now, **_kwargs):
        for poll in self.rows.values():
            if source_action_id and poll.source_action_id == source_action_id:
                return poll, False
        poll = Poll(
            uuid4(),
            scope,
            len(self.rows) + 1,
            creator.ref.key,
            creator.display_name,
            draft.question,
            draft.options,
            draft.multiple,
            draft.anonymous,
            "sending",
            draft.close_at,
            now,
            source_action_id=source_action_id,
        )
        self.rows[poll.id] = poll
        return poll, True

    async def mark_open(self, scope, poll_id, message_id, poll_id_native):
        self.rows[poll_id] = replace(
            self.rows[poll_id],
            status="open",
            platform_message_id=message_id,
            platform_poll_id=poll_id_native,
        )
        return True

    async def mark_failed(self, scope, poll_id):
        self.rows[poll_id] = replace(self.rows[poll_id], status="failed")

    async def fail_stale_sending(self, now):
        count = 0
        for poll in list(self.rows.values()):
            if poll.status == "sending" and poll.created_at < now - timedelta(minutes=10):
                self.rows[poll.id] = replace(poll, status="failed", closed_at=now)
                count += 1
        return count

    async def list_open(self, scope):
        return [p for p in self.rows.values() if p.scope == scope and p.status == "open"]

    async def get_by_number(self, scope, number):
        return next(
            (p for p in self.rows.values() if p.scope == scope and p.number == number), None
        )

    async def get_by_message(self, scope, message_id):
        return next(
            (
                p
                for p in self.rows.values()
                if p.scope == scope and p.platform_message_id == message_id
            ),
            None,
        )

    async def find_open_by_platform_poll(self, platform, poll_id):
        return next(
            (
                p
                for p in self.rows.values()
                if p.scope.chat.platform == platform
                and p.platform_poll_id == poll_id
                and p.status == "open"
            ),
            None,
        )

    async def replace_votes(self, poll, voter, option_ids, now):
        self.ballots = [
            v for v in self.ballots if (v["poll_id"], v["voter_key"]) != (poll.id, voter.ref.key)
        ]
        self.ballots.extend(
            dict(
                poll_id=poll.id,
                voter_key=voter.ref.key,
                voter_name=voter.display_name or f"@{voter.username}",
                option_index=i,
            )
            for i in option_ids
        )

    async def votes(self, scope, poll_id):
        return [v for v in self.ballots if v["poll_id"] == poll_id]

    async def claim_close(self, poll_id, now, limit, *, scope=None, allowed_chat_keys=None):
        self.claimed += 1
        claims = []
        for poll in list(self.rows.values()):
            if (
                poll.status != "open"
                or (allowed_chat_keys is not None and poll.scope.chat_key not in allowed_chat_keys)
                or (poll.lease_until and poll.lease_until > now)
                or (poll_id is not None and (poll.id != poll_id or poll.scope != scope))
                or (poll_id is None and (poll.close_at is None or poll.close_at > now))
            ):
                continue
            claimed = replace(
                poll,
                lease_token=uuid4(),
                lease_until=now + timedelta(seconds=60),
                close_at=poll.close_at if poll_id is None else now,
            )
            self.rows[poll.id] = claimed
            claims.append(claimed)
            if len(claims) == limit:
                break
        return claims

    async def finish_close(self, scope, poll_id, token, counts, total, now):
        poll = self.rows[poll_id]
        if not self.finish_wins or poll.status != "open" or token != poll.lease_token:
            return False
        self.rows[poll_id] = replace(
            poll, status="closed", final_counts=counts, total_voters=total, closed_at=now
        )
        return True

    async def purge_older_than(self, cutoff):
        self.rows = {
            key: poll
            for key, poll in self.rows.items()
            if not (
                poll.status in {"closed", "failed"} and (poll.closed_at or poll.created_at) < cutoff
            )
        }

    async def record_close_failure(self, scope, poll_id, token):
        poll = self.rows[poll_id]
        if token != poll.lease_token:
            return None
        poll = replace(poll, close_failures=poll.close_failures + 1)
        self.rows[poll_id] = poll
        return poll.close_failures


@pytest.fixture
def setup_poll():
    from datetime import UTC, datetime

    from chatcore_polls.plugin import PollsPlugin
    from chatcore_polls.settings import PollsSettings

    now = [datetime(2026, 10, 31, tzinfo=UTC)]
    repo = FakeRepo(now[0])
    platform = FakePollPlatform()
    access = AccessPolicy([GROUP.key], [], owners=[user("owner").ref.key])
    plugin = PollsPlugin(PollsSettings(), access, platform, clock=lambda: now[0])
    plugin.service.repo = lambda stores: repo
    stores = memory_stores()
    # Service repository is replaced by the stateful fake above; keep core display preferences in
    # memory even though the poll plugin needs a non-null database marker.
    stores.db = object()
    stores.extra["display_time_service"] = DisplayTimeService(MemoryDisplayTimeStore())
    ctx = CommandContext(GROUP, user(), False, False, platform, stores, thread_id="7")
    return plugin, ctx, repo, now
