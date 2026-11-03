import hashlib
import json
import logging
from types import SimpleNamespace

import pytest
from chatcore.access import AccessPolicy
from chatcore.events import ChatRef, User, UserRef
from chatcore.plugin import CommandContext
from chatcore.tools.registry import TurnState
from chatcore_browse.client import STATUS_MESSAGES, RenderedPage
from chatcore_browse.plugin import BrowsePlugin
from chatcore_browse.repository import BrowseConflict
from chatcore_browse.service import BrowseService
from chatcore_browse.settings import BrowseSettings
from chatcore_browse.urls import normalize_url


class RecordingCache:
    def __init__(self, values=None):
        self.values = values or {}
        self.get_calls = []

    async def get(self, key):
        self.get_calls.append(key)
        return self.values.get(key)

    async def set(self, key, value, *, ttl_s):
        self.values[key] = value


class RecordingRepo:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.reads = 0

    async def settings(self, scope):
        self.reads += 1
        return {"rendered_enabled": self.enabled, "revision": 0}


class RecordingClient:
    def __init__(self, status="ok"):
        self.calls = []
        self.status = status

    async def render(self, url, *, max_response_bytes):
        self.calls.append((url, max_response_bytes))
        return RenderedPage(self.status, "https://example.com/final", "Title", "page text", False)


class Platform:
    name = "tg"

    async def is_member(self, chat, user):
        return False

    async def is_group_admin(self, chat, user):
        return False


def context(urls=(), *, is_dm=False, cache=None, fetches=0):
    chat = ChatRef("tg", "7" if is_dm else "-100")
    return SimpleNamespace(
        chat=chat,
        user=User(UserRef("tg", "7"), "Member"),
        is_dm=is_dm,
        thread_id=None,
        turn=TurnState(seen_urls=set(urls), fetches=fetches),
        stores=SimpleNamespace(db=object(), cache=cache or RecordingCache()),
    )


def service(
    client,
    enabled=True,
    *,
    settings=None,
    access=None,
    platform=None,
    repo=None,
):
    access = access or AccessPolicy(["tg:-100"], [])
    platform = platform or Platform()
    repo = repo or RecordingRepo(enabled)
    instance = BrowseService(
        settings or BrowseSettings(render_url="https://renderer.invalid"),
        access,
        platform,
        client,
    )
    instance.repo = lambda ctx: repo
    return instance, repo


async def test_render_tool_absent_without_renderer_and_present_with_it():
    off = BrowsePlugin(BrowseSettings(), object(), object())
    assert [tool.name for tool in off.tools()] == ["build_search_link"]
    on = BrowsePlugin(BrowseSettings(render_url="https://renderer.invalid"), object(), object())
    assert [tool.name for tool in on.tools()] == [
        "build_search_link",
        "configure_rendered_fetch",
        "fetch_rendered_page",
    ]


async def test_dm_scope_is_denied_before_repository_cache_or_client():
    client = RecordingClient()
    instance, repo = service(client)
    ctx = context(["https://example.com"], is_dm=True)
    result = await instance.fetch(ctx, "https://example.com")
    assert result.is_error
    assert repo.reads == 0
    assert ctx.stores.cache.get_calls == []
    assert client.calls == []


@pytest.mark.parametrize("stage", ["scope", "disabled", "url", "provenance", "limit"])
async def test_refusals_stop_at_the_expected_gate(stage):
    client = RecordingClient()
    settings = BrowseSettings(render_url="https://renderer.invalid", max_fetches_per_turn=2)
    access = AccessPolicy([] if stage == "scope" else ["tg:-100"], [])
    instance, repo = service(client, enabled=stage != "disabled", settings=settings, access=access)
    cache = RecordingCache()
    fetches = settings.max_fetches_per_turn if stage == "limit" else 0
    urls = [] if stage == "provenance" else ["https://example.com"]
    ctx = context(urls, cache=cache, fetches=fetches)
    url = "https://user:pass@example.com" if stage == "url" else "https://example.com"
    result = await instance.fetch(ctx, url)
    assert result.is_error
    assert repo.reads == (0 if stage == "scope" else 1)
    assert cache.get_calls == []
    assert client.calls == []


async def test_provenance_normalization_and_http_ipv6_upgrade():
    client = RecordingClient()
    instance, _ = service(client)
    unseen = await instance.fetch(context(), "https://example.com")
    assert unseen.is_error and not client.calls
    ctx = context(["HTTPS://Example.COM.:443"])
    result = await instance.fetch(ctx, "https://example.com")
    assert not result.is_error and result.untrusted
    assert client.calls[-1][0] == "https://example.com"

    ipv6_url = "http://[2606:4700:4700::1111]:80/path"
    await instance.fetch(context([ipv6_url]), ipv6_url)
    assert client.calls[-1][0] == "https://[2606:4700:4700::1111]/path"
    with_port = "http://[2606:4700:4700::1111]:443/path"
    await instance.fetch(context([with_port]), with_port)
    assert client.calls[-1][0] == "https://[2606:4700:4700::1111]:443/path"


async def test_cache_hit_skips_client_and_respects_current_max_chars():
    url = "https://example.com"
    normalized = normalize_url(url)
    key = "browse:page:" + hashlib.sha256(normalized.encode()).hexdigest()
    cached = (
        "Title: cached\nURL: https://example.com/final\n"
        "Rendered by a headless browser; text only.\n\n" + "x" * 200
    )
    cache = RecordingCache({key: json.dumps({"content": cached}).encode()})
    client = RecordingClient()
    settings = BrowseSettings(render_url="https://renderer.invalid", max_chars=50)
    instance, _ = service(client, settings=settings)
    result = await instance.fetch(context([url], cache=cache), url)
    assert result.untrusted
    assert len(result.content) <= 50
    assert result.content.endswith("[Truncated]")
    assert client.calls == []
    assert len(cache.get_calls) == 1


async def test_success_result_is_untrusted_and_cached():
    client = RecordingClient()
    cache = RecordingCache()
    instance, _ = service(client)
    result = await instance.fetch(
        context(["https://example.com"], cache=cache), "https://example.com"
    )
    assert not result.is_error and result.untrusted
    assert result.content.startswith("Title: Title\nURL: https://example.com/final\n")
    assert len(cache.values) == 1
    assert len(client.calls) == 1


async def test_render_failure_log_has_cause_without_url_path(caplog):
    class FailingClient:
        async def render(self, url, *, max_response_bytes):
            try:
                raise ValueError("hidden details")
            except ValueError as cause:
                raise RuntimeError("request failed") from cause

    instance, _ = service(FailingClient())
    caplog.set_level(logging.WARNING, logger="chatcore_browse.service")
    await instance.fetch(
        context(["https://example.com/private/path?q=secret"]),
        "https://example.com/private/path?q=secret",
    )
    record = next(record for record in caplog.records if "render failed" in record.message)
    assert record.message == "render failed host=example.com error=RuntimeError cause=ValueError"
    assert "private" not in record.message and "secret" not in record.message


@pytest.mark.parametrize("status", ["blocked_by_site", "timeout", "too_large", "busy", "error"])
async def test_non_ok_results_are_not_cached_across_repeated_requests(status):
    client = RecordingClient(status)
    cache = RecordingCache()
    instance, _ = service(client)
    for _ in range(2):
        result = await instance.fetch(
            context(["https://example.com"], cache=cache), "https://example.com"
        )
        assert result.is_error
        if status in STATUS_MESSAGES:
            assert result.content == STATUS_MESSAGES[status]
        else:
            assert result.content == "The page could not be rendered."
        assert cache.values == {}
    assert len(client.calls) == 2


async def test_blocked_site_is_reported_plainly_and_not_retried():
    client = RecordingClient("blocked_by_site")
    cache = RecordingCache()
    instance, _ = service(client)
    result = await instance.fetch(
        context(["https://example.com"], cache=cache), "https://example.com"
    )
    assert len(client.calls) == 1
    assert (
        result.content
        == "This site blocked automated access, so I can't read it. Please open the link yourself."
    )
    assert result.is_error
    assert cache.values == {}


async def test_render_limit_is_distinct_from_shared_fetch_limit():
    client = RecordingClient()
    instance, _ = service(client)
    ctx = context(["https://example.com"])
    for _ in range(2):
        assert (await instance.fetch(ctx, "https://example.com")).untrusted
    limited = await instance.fetch(ctx, "https://example.com")
    assert limited.is_error
    assert len(client.calls) == 1


async def test_configure_refuses_non_manager_before_repository_read():
    instance, repo = service(RecordingClient())
    ctx = context()
    with pytest.raises(PermissionError):
        await instance.request_configure(ctx, True)
    assert repo.reads == 0


class OwnerPlatform(Platform):
    pass


class ConfigureRepo:
    async def settings(self, scope):
        return {"revision": 4, "rendered_enabled": False}

    async def configure(self, scope, *, enabled, actor, expected_revision):
        if expected_revision != 4:
            raise BrowseConflict("settings changed")
        return {"rendered_enabled": enabled}


async def test_configure_manager_confirmation_and_stale_revision():
    access = AccessPolicy([], [], owners=["tg:7"])
    platform = OwnerPlatform()
    instance, _ = service(RecordingClient(), access=access, platform=platform, repo=ConfigureRepo())
    ctx = context()
    action = await instance.request_configure(ctx, True)
    assert action.plugin == "browse" and action.kind == "configure"
    assert action.payload["revision"] == 4
    assert "sandboxed headless browser" in action.summary
    command_ctx = CommandContext(ctx.chat, ctx.user, False, False, platform, ctx.stores)
    assert (
        await instance.commit(action, command_ctx) == "Rendered page reading enabled for tg:-100."
    )
    action.payload["revision"] = 3
    assert await instance.commit(action, command_ctx) == "Settings changed, nothing done."


async def test_plugin_command_and_action_handler_only_with_renderer():
    plugin = BrowsePlugin(BrowseSettings(render_url="https://renderer.invalid"), object(), object())
    assert [command.name for command in plugin.commands()] == ["browsetools"]
    assert set(plugin.action_handlers()) == {"configure"}


async def test_purge_user_nulls_actor_only_in_the_given_chat():
    class FakeDb:
        def __init__(self):
            self.calls = []

        async def execute(self, sql, *args):
            self.calls.append((sql, args))

    db = FakeDb()
    plugin = BrowsePlugin(BrowseSettings(), object(), object())
    await plugin.purge_user(ChatRef("tg", "-100"), UserRef("tg", "7"), SimpleNamespace(db=db))
    assert len(db.calls) == 1
    sql, args = db.calls[0]
    assert "UPDATE browse_settings SET updated_by=NULL" in sql
    assert args == ("tg:-100", "tg:7")
