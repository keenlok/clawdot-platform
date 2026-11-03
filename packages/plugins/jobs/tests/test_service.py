from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from chatcore.access import AccessPolicy
from chatcore.display_time_store import display_service
from chatcore.events import ChatRef, User, UserRef
from chatcore.tools.registry import TurnState
from chatcore_jobs.models import Actor, Job, JobKind, JobStatus, Scope
from chatcore_jobs.service import JobsService
from chatcore_jobs.settings import JobsSettings


class Repo:
    def __init__(self):
        self.configuration = {
            "reminders_enabled": True,
            "watches_enabled": True,
            "max_jobs_per_user": 10,
            "max_jobs_per_chat": 100,
            "minimum_interval_seconds": 300,
            "revision": 4,
        }
        self.created = []
        self.job = None

    async def settings(self, scope):
        return self.configuration.copy()

    async def create(self, *args, **kwargs):
        self.created.append((args, kwargs))
        scope, actor, _action_id, kind, payload, due = args
        return Job(
            uuid4(),
            scope,
            actor,
            kind,
            JobStatus.ACTIVE,
            payload,
            kwargs["now"],
            due,
            revision=kwargs["revision"],
        )

    async def get_job(self, scope, ident):
        return self.job

    async def restore(self, scope, actor, kind, job_id, **kwargs):
        if self.job is None or self.job.revision != kwargs["expected_revision"]:
            return None
        self.job = replace(
            self.job,
            status=JobStatus.ACTIVE,
            revision=self.job.revision + 1,
            terminal_at=None,
            last_delivery_state=None,
            next_run_at=max(self.job.next_run_at, kwargs["now"]),
        )
        return self.job

    async def count_active(self, scope, kind):
        return 0

    async def count_active_rendered_watches(self):
        return 0

    async def configure(self, scope, **kwargs):
        result = self.configuration.copy()
        result["_cancelled_jobs"] = 0
        return result


class Platform:
    name = "tg"

    def __init__(self, *, admin=True):
        self.is_group_admin = AsyncMock(return_value=admin)
        self.is_member = AsyncMock(return_value=True)


def context(repo, *, thread_id="7", is_dm=False, user_id="7", global_admin=False):
    chat = ChatRef("tg", "-100" if not is_dm else "7")
    user_ref = UserRef("tg", user_id)
    platform = Platform()
    ctx = SimpleNamespace(
        chat=chat,
        user=User(user_ref, "Ada"),
        is_dm=is_dm,
        is_admin=global_admin,
        thread_id=thread_id,
        platform=platform,
        stores=SimpleNamespace(db=None, extra={}),
        turn=TurnState(),
        confirm=None,
        command_form=lambda name: f"/{name}" if is_dm else f"/{name}@sbot",
    )
    return ctx


def make_service(repo, platform=None):
    service = JobsService(
        JobsSettings(dm_enabled=True),
        AccessPolicy(["tg:-100"], ["tg:admin"]),
        platform or Platform(),
        clock=lambda: datetime(2026, 10, 31, tzinfo=UTC),
    )
    service.repo = lambda stores: repo
    return service


def test_restore_window_setting_is_positive():
    assert JobsSettings().restore_window_seconds == 86400
    with pytest.raises(ValueError):
        JobsSettings(restore_window_seconds=0)


@pytest.mark.asyncio
async def test_rendered_watch_requires_permission_and_describes_cost_behavior():
    repo = Repo()
    service = make_service(repo)
    service.rendered_available = True
    service.rendered_allowed = AsyncMock(return_value=True)
    ctx = context(repo)
    action = await service.request_create(
        ctx,
        JobKind.WATCH,
        {"url": "http://example.com/path", "interval_seconds": 3600, "rendered": True},
    )
    assert action.payload["data"] == {
        "url": "https://example.com/path",
        "interval_seconds": 3600,
        "mode": "rendered",
    }
    assert "rendered (headless browser) watch" in action.summary
    assert "First check is silent" in action.summary
    assert "only readable-text changes" in action.summary
    assert "block automated access" in action.summary
    assert "30 days" in action.summary
    service.rendered_allowed = AsyncMock(return_value=False)
    with pytest.raises(ValueError, match="disabled"):
        await service.request_create(
            ctx,
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 3600, "rendered": True},
        )


@pytest.mark.asyncio
async def test_rendered_watch_rejects_unavailable_capability_and_interval_before_provenance():
    repo = Repo()
    service = make_service(repo)
    ctx = context(repo)
    with pytest.raises(ValueError, match="unavailable"):
        await service.request_create(
            ctx,
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 60, "rendered": True},
            url_provenance=True,
        )
    service.rendered_available = True
    service.rendered_allowed = AsyncMock(return_value=True)
    with pytest.raises(ValueError, match="at least 3600"):
        await service.request_create(
            ctx,
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 60, "rendered": True},
            url_provenance=True,
        )


@pytest.mark.asyncio
async def test_rendered_watch_fails_closed_when_its_interval_window_is_empty():
    repo = Repo()
    settings = JobsSettings(maximum_watch_interval_seconds=600)
    service = JobsService(
        settings,
        AccessPolicy(["tg:-100"], ["tg:admin"]),
        Platform(),
        clock=lambda: datetime(2026, 10, 31, tzinfo=UTC),
        rendered_allowed=AsyncMock(return_value=True),
        rendered_available=True,
    )
    service.repo = lambda _stores: repo
    with pytest.raises(ValueError, match="Rendered watches are unavailable"):
        await service.request_create(
            context(repo),
            JobKind.WATCH,
            {"url": "https://example.com", "interval_seconds": 600, "rendered": True},
        )


@pytest.mark.asyncio
async def test_reminder_request_is_pending_and_contains_exact_time_zone_and_topic():
    repo = Repo()
    ctx = context(repo)
    action = await make_service(repo).request_create(
        ctx,
        JobKind.REMINDER,
        {
            "text": "call mom",
            "local_time": "2026-11-01T09:00:00",
            "timezone": "Asia/Singapore",
        },
    )
    assert not repo.created
    assert action.kind == "create"
    assert action.thread_id == "7"
    assert "Asia/Singapore" in action.summary and "+08:00" in action.summary
    assert action.payload["scope"]["thread_id"] == "7"


@pytest.mark.asyncio
async def test_create_reminder_echo_uses_display_zone_and_full_id():
    repo = Repo()
    ctx = context(repo)
    await display_service(ctx.stores).set_zone(
        ctx.chat, ctx.user.ref, is_dm=ctx.is_dm, zone="Asia/Singapore"
    )
    service = make_service(repo)
    action = await service.request_create(
        ctx,
        JobKind.REMINDER,
        {"text": "call mom", "local_time": "2026-11-01T09:00:00", "timezone": "UTC"},
    )
    echo = await service.commit(action, ctx)
    assert echo.startswith("Reminder set for 2026-11-01 17:00:00 Asia/Singapore (UTC+08:00)")
    assert '"call mom" (id ' in echo and echo.endswith('Say "undo" to cancel it.')
    assert len(repo.created) == 1


@pytest.mark.asyncio
async def test_restore_reminder_requires_user_cancel_and_reactivates_with_echo():
    repo = Repo()
    service = make_service(repo)
    ctx = context(repo)
    ident = uuid4()
    repo.job = Job(
        ident,
        Scope("tg:-100", False, "7"),
        Actor("tg:7"),
        JobKind.REMINDER,
        JobStatus.CANCELLED,
        {"text": "call mom"},
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        revision=3,
        last_delivery_state="cancelled_by_user",
        terminal_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    action = await service.request_restore(ctx, JobKind.REMINDER, str(ident))
    assert action is not None and action.kind == "restore"
    assert action.payload["revision"] == 3 and action.payload["settings_revision"] == 4
    echo = await service.commit(action, context(repo))
    assert echo == (
        f'Restored reminder {ident}: "call mom" at 2026-11-02 00:00:00 UTC '
        '(UTC+00:00). Say "undo" to cancel it.'
    )
    assert repo.job.status is JobStatus.ACTIVE


@pytest.mark.asyncio
async def test_restore_refuses_config_cancel_and_past_reminder():
    repo = Repo()
    service = make_service(repo)
    ctx = context(repo)
    base = Job(
        uuid4(),
        Scope("tg:-100", False, "7"),
        Actor("tg:7"),
        JobKind.REMINDER,
        JobStatus.CANCELLED,
        {"text": "call mom"},
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        revision=2,
        last_delivery_state="interval_floor_raised",
        terminal_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    repo.job = base
    assert await service.request_restore(ctx, JobKind.REMINDER, str(base.id)) is None
    repo.job = replace(
        base,
        last_delivery_state="cancelled_by_user",
        next_run_at=datetime(2026, 10, 30, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="time has passed"):
        await service.request_restore(ctx, JobKind.REMINDER, str(base.id))


@pytest.mark.asyncio
async def test_restore_requires_owner_or_current_manager_and_enforces_window():
    repo = Repo()
    service = make_service(repo, Platform(admin=False))
    ctx = context(repo)
    target = Job(
        uuid4(),
        Scope("tg:-100", False, "7"),
        Actor("tg:8"),
        JobKind.REMINDER,
        JobStatus.CANCELLED,
        {"text": "call mom"},
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        revision=2,
        last_delivery_state="cancelled_by_user",
        terminal_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    repo.job = target
    assert await service.request_restore(ctx, JobKind.REMINDER, str(target.id)) is None
    service.platform = Platform(admin=True)
    action = await service.request_restore(ctx, JobKind.REMINDER, str(target.id))
    assert action is not None and action.payload["all_members"] is True
    repo.job = replace(target, terminal_at=datetime(2026, 10, 29, 23, 59, tzinfo=UTC))
    assert await service.request_restore(ctx, JobKind.REMINDER, str(target.id)) is None


@pytest.mark.asyncio
async def test_restore_disabled_error_names_jobtools_command():
    repo = Repo()
    repo.configuration["watches_enabled"] = False
    service = make_service(repo)
    target = Job(
        uuid4(),
        Scope("tg:-100", False, "7"),
        Actor("tg:7"),
        JobKind.WATCH,
        JobStatus.CANCELLED,
        {"url": "https://example.test", "interval_seconds": 600},
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        revision=2,
        last_delivery_state="cancelled_by_user",
        terminal_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    repo.job = target
    with pytest.raises(ValueError, match="/jobtools@sbot watches on"):
        await service.request_restore(context(repo), JobKind.WATCH, str(target.id))


@pytest.mark.asyncio
async def test_create_watch_echo_describes_url_interval_and_undo(caplog):
    repo = Repo()
    service = make_service(repo)
    ctx = context(repo)
    ctx.turn.seen_urls.add("https://example.test/private/path?data=sample")
    action = await service.request_create(
        ctx,
        JobKind.WATCH,
        {"url": "https://example.test/private/path?data=sample", "interval_seconds": 600},
        url_provenance=True,
    )
    echo = await service.commit(action, context(repo))
    assert echo.startswith(
        "Watching https://example.test/private/path?data=sample every 600 seconds (id "
    )
    assert "first check is silent" in echo and echo.endswith('Say "undo" to stop it.')
    assert "https://example.test/private/path?data=sample" not in caplog.text


@pytest.mark.asyncio
async def test_disabled_job_tool_errors_name_enable_command():
    repo = Repo()
    repo.configuration["reminders_enabled"] = False
    repo.configuration["watches_enabled"] = False
    service = make_service(repo)
    ctx = context(repo)
    with pytest.raises(ValueError, match="/jobtools@sbot reminders on"):
        await service.request_create(
            ctx,
            JobKind.REMINDER,
            {"text": "call", "local_time": "2026-11-01T09:00:00", "timezone": "UTC"},
        )
    with pytest.raises(ValueError, match="/jobtools@sbot watches on"):
        await service.request_create(
            ctx,
            JobKind.WATCH,
            {"url": "https://example.test", "interval_seconds": 600},
        )


@pytest.mark.asyncio
async def test_commit_preserves_stored_topic_when_button_context_has_no_topic():
    repo = Repo()
    service = make_service(repo)
    initial = context(repo)
    action = await service.request_create(
        initial,
        JobKind.REMINDER,
        {
            "text": "call mom",
            "local_time": "2026-11-01T09:00:00",
            "timezone": "Asia/Singapore",
        },
    )
    confirmed = context(repo, thread_id=None)
    text = await service.commit(action, confirmed)
    assert text.startswith('Reminder set for 2026-11-01 01:00:00 UTC (UTC+00:00): "call mom"')
    args, kwargs = repo.created[0]
    assert args[0] == Scope("tg:-100", False, "7")


@pytest.mark.asyncio
async def test_outsider_confirmation_and_settings_revision_change_do_not_write():
    repo = Repo()
    service = make_service(repo)
    action = await service.request_create(
        context(repo),
        JobKind.REMINDER,
        {
            "text": "call mom",
            "local_time": "2026-11-01T09:00:00",
            "timezone": "Asia/Singapore",
        },
    )
    outsider = context(repo, user_id="9")
    assert "Nothing changed" in await service.commit(action, outsider)
    assert not repo.created
    repo.configuration["revision"] += 1
    assert "changed" in await service.commit(action, context(repo))
    assert not repo.created


@pytest.mark.asyncio
async def test_watch_tool_requires_url_provenance_and_rejects_credentials():
    repo = Repo()
    service = make_service(repo)
    ctx = context(repo)
    with pytest.raises(ValueError, match="appeared"):
        await service.request_create(
            ctx,
            JobKind.WATCH,
            {"url": "https://example.com/x", "interval_seconds": 300},
            url_provenance=True,
        )
    ctx.turn.seen_urls.add("https://user:pass@example.com/x")
    with pytest.raises(ValueError, match="credentials"):
        await service.request_create(
            ctx,
            JobKind.WATCH,
            {"url": "https://user:pass@example.com/x", "interval_seconds": 300},
            url_provenance=True,
        )


@pytest.mark.asyncio
async def test_bool_is_not_watch_interval():
    repo = Repo()
    with pytest.raises(ValueError):
        await make_service(repo).request_create(
            context(repo), JobKind.WATCH, {"url": "https://example.com", "interval_seconds": True}
        )


@pytest.mark.asyncio
async def test_disable_then_reenable_invalidates_pending_create():
    repo = Repo()
    service = make_service(repo)
    action = await service.request_create(
        context(repo),
        JobKind.REMINDER,
        {
            "text": "call mom",
            "local_time": "2026-11-01T09:00:00",
            "timezone": "Asia/Singapore",
        },
    )
    repo.configuration["reminders_enabled"] = False
    repo.configuration["revision"] += 1
    repo.configuration["reminders_enabled"] = True
    repo.configuration["revision"] += 1
    reply = await service.commit(action, context(repo))
    assert "changed" in reply
    assert not repo.created


@pytest.mark.asyncio
async def test_group_configuration_confirmation_rechecks_live_admin_role():
    repo = Repo()
    platform = Platform(admin=True)
    service = make_service(repo, platform)
    action = await service.request_configure(context(repo), JobKind.WATCH, False, {})
    platform.is_group_admin.return_value = False
    reply = await service.commit(action, context(repo))
    assert "authorization" in reply.lower()
    assert not repo.created


@pytest.mark.asyncio
async def test_global_admin_status_does_not_bypass_live_group_role():
    repo = Repo()
    platform = Platform(admin=False)
    service = JobsService(
        JobsSettings(),
        AccessPolicy(["tg:-100"], ["tg:7"]),
        platform,
        clock=lambda: datetime(2026, 10, 31, tzinfo=UTC),
    )
    service.repo = lambda stores: repo
    with pytest.raises(PermissionError):
        await service.request_configure(context(repo, global_admin=True), JobKind.WATCH, True, {})


@pytest.mark.asyncio
async def test_confirmation_after_requested_time_passes_does_not_reschedule():
    repo = Repo()
    clock = [datetime(2026, 10, 31, tzinfo=UTC)]
    service = JobsService(
        JobsSettings(), AccessPolicy(["tg:-100"], []), Platform(), clock=lambda: clock[0]
    )
    service.repo = lambda stores: repo
    action = await service.request_create(
        context(repo),
        JobKind.REMINDER,
        {
            "text": "call mom",
            "local_time": "2026-11-01T09:00:00",
            "timezone": "Asia/Singapore",
        },
    )
    clock[0] = datetime(2026, 11, 1, 1, tzinfo=UTC)
    reply = await service.commit(action, context(repo))
    assert "invalid" in reply
    assert not repo.created


@pytest.mark.asyncio
async def test_forged_pending_topic_cannot_redirect_creation():
    repo = Repo()
    service = make_service(repo)
    action = await service.request_create(
        context(repo),
        JobKind.REMINDER,
        {
            "text": "call mom",
            "local_time": "2026-11-01T09:00:00",
            "timezone": "Asia/Singapore",
        },
    )
    action.payload["scope"]["thread_id"] = "8"
    reply = await service.commit(action, context(repo, thread_id=None))
    assert "Authorization" in reply
    assert not repo.created


@pytest.mark.asyncio
async def test_owner_can_cancel_dm_job_after_global_disable():
    from uuid import uuid4

    repo = Repo()
    ident = uuid4()
    repo.job = Job(
        ident,
        Scope("tg:7", True),
        Actor("tg:7"),
        JobKind.REMINDER,
        JobStatus.ACTIVE,
        {"text": "private"},
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    service = JobsService(
        JobsSettings(dm_enabled=False),
        AccessPolicy(["tg:-100"], []),
        Platform(),
        clock=lambda: datetime(2026, 10, 31, tzinfo=UTC),
    )
    service.repo = lambda stores: repo
    ctx = context(repo, thread_id=None, is_dm=True)
    action = await service.request_cancel(ctx, JobKind.REMINDER, str(ident))
    assert action is not None
    assert action.payload["scope"]["is_dm"] is True


@pytest.mark.asyncio
async def test_pending_dm_cancel_commits_after_global_tool_disable():
    from uuid import uuid4

    repo = Repo()
    ident = uuid4()
    repo.job = Job(
        ident,
        Scope("tg:7", True),
        Actor("tg:7"),
        JobKind.REMINDER,
        JobStatus.ACTIVE,
        {"text": "private"},
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    )

    async def cancel(*_args, **_kwargs):
        repo.cancelled = True
        return True

    repo.cancel = cancel
    service = JobsService(
        JobsSettings(dm_enabled=True),
        AccessPolicy(["tg:-100"], []),
        Platform(),
        clock=lambda: datetime(2026, 10, 31, tzinfo=UTC),
    )
    service.repo = lambda _stores: repo
    ctx = context(repo, is_dm=True, thread_id=None)
    action = await service.request_cancel(ctx, JobKind.REMINDER, str(ident))
    service.settings.dm_enabled = False
    result = await service.commit(action, ctx)
    assert result.startswith("Cancelled reminder ")
    assert repo.cancelled


@pytest.mark.asyncio
async def test_watch_confirmation_rechecks_interval_after_floor_changes():
    repo = Repo()
    service = make_service(repo)
    ctx = context(repo)
    ctx.turn.seen_urls.add("https://example.com")
    action = await service.request_create(
        ctx,
        JobKind.WATCH,
        {
            "url": "https://example.com",
            "interval_seconds": 300,
        },
        url_provenance=True,
    )
    repo.configuration["minimum_interval_seconds"] = 900
    result = await service.commit(action, ctx)
    assert "interval" in result.lower() or "changed" in result.lower()
    assert not repo.created


@pytest.mark.asyncio
async def test_all_members_list_requires_live_role_and_keeps_current_topic():
    repo = Repo()
    repo.listed = None

    async def list_jobs(scope, actor, kind, *, all_members=False):
        repo.listed = (scope, actor, kind, all_members)
        return []

    repo.list_jobs = list_jobs
    platform = Platform(admin=True)
    platform.is_group_admin.side_effect = lambda _chat, user: user.id == "7"
    service = make_service(repo, platform)
    ctx = context(repo)
    denied = context(repo, user_id="9")
    assert "administrator" in await service.list_jobs(denied, JobKind.REMINDER, True)
    assert repo.listed is None
    assert await service.list_jobs(ctx, JobKind.REMINDER, True) == "No jobs found."
    assert repo.listed[0] == Scope("tg:-100", False, "7")
    assert repo.listed[3] is True


@pytest.mark.asyncio
async def test_job_list_formats_next_run_in_effective_display_zone():
    repo = Repo()
    ctx = context(repo)
    run_at = datetime(2026, 11, 1, 1, tzinfo=UTC)
    repo.list_jobs = AsyncMock(
        return_value=[
            SimpleNamespace(
                id="job-1",
                status=JobStatus.ACTIVE,
                next_run_at=run_at,
                last_delivery_state=None,
                payload={},
            )
        ]
    )
    await display_service(ctx.stores).set_zone(
        ctx.chat, ctx.user.ref, is_dm=False, zone="Asia/Singapore"
    )

    result = await make_service(repo).list_jobs(ctx, JobKind.REMINDER)

    assert "2026-11-01 09:00:00 Asia/Singapore (UTC+08:00)" in result
    assert repo.list_jobs.await_args.args[0].chat_key == ctx.chat.key
    assert run_at == datetime(2026, 11, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_display_preference_does_not_change_explicit_reminder_instant_or_watch_interval():
    repo = Repo()
    ctx = context(repo)
    service = make_service(repo)
    reminder = {
        "text": "call mom",
        "local_time": "2026-11-01T09:00:00",
        "timezone": "Asia/Singapore",
    }
    original = await service.request_create(ctx, JobKind.REMINDER, reminder)
    await display_service(ctx.stores).set_zone(
        ctx.chat, ctx.user.ref, is_dm=False, zone="America/New_York"
    )
    after_change = await service.request_create(ctx, JobKind.REMINDER, reminder)
    watch = await service.request_create(
        ctx,
        JobKind.WATCH,
        {"url": "https://example.com", "interval_seconds": 3600},
    )

    assert original.payload["due_at"] == after_change.payload["due_at"]
    assert original.payload["due_at"] == "2026-11-01T01:00:00+00:00"
    assert watch.payload["data"]["interval_seconds"] == 3600


@pytest.mark.asyncio
async def test_owner_and_assigned_delegate_can_manage_only_assigned_group():
    from chatcore.administration.store import AdminChange, MemoryAdministrationStore

    group = ChatRef("tg", "-100")
    other_group = ChatRef("tg", "-200")
    owner = UserRef("tg", "1")
    delegate = UserRef("tg", "7")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    access = AccessPolicy([group.key, other_group.key], [], owners=[owner.key], store=store)
    platform = Platform(admin=False)
    repo = Repo()
    service = make_service(repo, platform)
    service.access = access

    delegate_action = await service.request_configure(
        context(repo, user_id=delegate.id), JobKind.WATCH, True, {}
    )
    assert delegate_action.kind == "configure"
    owner_action = await service.request_configure(
        context(repo, user_id=owner.id), JobKind.WATCH, True, {}
    )
    assert owner_action.kind == "configure"
    other_context = context(repo, user_id=delegate.id)
    other_context.chat = other_group
    with pytest.raises(PermissionError):
        await service.request_configure(other_context, JobKind.WATCH, True, {})


@pytest.mark.asyncio
async def test_delegated_setup_revocation_before_confirmation_denies():
    from chatcore.administration.store import AdminChange, MemoryAdministrationStore

    group = ChatRef("tg", "-100")
    owner, delegate = UserRef("tg", "1"), UserRef("tg", "7")
    store = MemoryAdministrationStore()
    granted = await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    access = AccessPolicy([group.key], [], store=store)
    repo, platform = Repo(), Platform(admin=False)
    service = make_service(repo, platform)
    service.access = access
    ctx = context(repo, user_id=delegate.id)
    action = await service.request_configure(ctx, JobKind.WATCH, True, {})
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=granted.revision,
        change=AdminChange("revoke", group, user=delegate),
    )
    reply = await service.commit(action, ctx)
    assert "authorization" in reply.lower()


@pytest.mark.asyncio
async def test_disabling_confirmation_names_permanent_cancellation_and_count():
    repo = Repo()
    repo.count_active = AsyncMock(return_value=3)
    action = await make_service(repo).request_configure(context(repo), JobKind.WATCH, False, {})
    assert "permanently cancels 3 active watch job(s)" in action.summary
    assert "re-enabling will not restore them" in action.summary


@pytest.mark.asyncio
async def test_assigned_manager_all_members_list_and_cancel_stay_in_current_topic():
    from chatcore.administration.store import AdminChange, MemoryAdministrationStore

    group, owner, delegate = ChatRef("tg", "-100"), UserRef("tg", "1"), UserRef("tg", "7")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    access = AccessPolicy([group.key], [], store=store)
    repo = Repo()
    service = make_service(repo, Platform(admin=False))
    service.access = access
    ctx = context(repo, user_id=delegate.id, thread_id="7")
    repo.listed = None

    async def list_jobs(scope, actor, kind, *, all_members=False):
        repo.listed = (scope, actor, kind, all_members)
        return []

    repo.list_jobs = list_jobs
    assert await service.list_jobs(ctx, JobKind.REMINDER, True) == "No jobs found."
    assert repo.listed == (
        Scope(group.key, False, "7"),
        Actor(delegate.key),
        JobKind.REMINDER,
        True,
    )

    target = Job(
        __import__("uuid").uuid4(),
        Scope(group.key, False, "7"),
        Actor(owner.key),
        JobKind.REMINDER,
        JobStatus.ACTIVE,
        {"text": "x"},
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    repo.job = target
    cancelled = []

    async def cancel(scope, actor, kind, ident, **kwargs):
        cancelled.append((scope, actor, kind, ident, kwargs))
        return True

    repo.cancel = cancel
    action = await service.request_cancel(ctx, JobKind.REMINDER, str(target.id))
    assert action is not None and action.payload["all_members"] is True
    reply = await service.commit(action, ctx)
    assert reply.startswith("Cancelled reminder ")
    assert cancelled[0][0] == Scope(group.key, False, "7")
    assert cancelled[0][4]["all_members"] is True


@pytest.mark.asyncio
async def test_group_manager_cannot_list_or_cancel_other_users_dm_jobs():
    from chatcore.administration.store import AdminChange, MemoryAdministrationStore

    group, owner, delegate = ChatRef("tg", "-100"), UserRef("tg", "1"), UserRef("tg", "7")
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    repo, platform = Repo(), Platform(admin=True)
    service = make_service(repo, platform)
    service.access = AccessPolicy([group.key], [], store=store)
    ctx = context(repo, user_id=delegate.id, thread_id=None, is_dm=True)
    ctx.group_for_dm = AsyncMock(side_effect=AssertionError("DM jobs do not infer a group"))
    repo.job = Job(
        __import__("uuid").uuid4(),
        Scope(delegate.key, True),
        Actor("tg:9"),
        JobKind.REMINDER,
        JobStatus.ACTIVE,
        {"text": "private"},
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    repo.listed = None
    assert "administrator" in await service.list_jobs(ctx, JobKind.REMINDER, True)
    assert repo.listed is None
    assert await service.request_cancel(ctx, JobKind.REMINDER, str(repo.job.id)) is None
    ctx.group_for_dm.assert_not_awaited()
    platform.is_group_admin.assert_not_awaited()


async def test_disabled_call_time_error_matches_routing_hint():
    from chatcore.skills import render_hint
    from chatcore_jobs.models import JobKind
    from chatcore_jobs.service import DISABLED_HINTS

    ctx = context(Repo())
    for kind in JobKind:
        text = JobsService._disabled_text(kind, ctx)
        assert text == render_hint(DISABLED_HINTS[kind], ctx.command_form)
        assert "{cmd:" not in text and "/jobtools@sbot" in text
        assert "setup manager" in text
