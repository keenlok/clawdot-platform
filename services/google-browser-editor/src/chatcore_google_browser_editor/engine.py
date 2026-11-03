"""One fresh, guarded Chromium context per confirmed editing job."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from chatcore.security.egress_proxy import EgressPolicy, EgressProxy, RenderBudget
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .docs_ui import DocsUiAdapter
from .egress import AllowlistedEgressPolicy
from .models import CreateJobRequest, editor_url
from .resource_hosts import RESOURCE_HOSTS
from .settings import WorkerSettings
from .worker import CancelledAtBoundary, WorkerFault

PRODUCTION_EDITOR_ORIGIN = "https://docs.google.com"
_PROXY_ENV_KEYS = {
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
}


def clean_browser_environment(home: str) -> dict[str, str]:
    """Drop ambient proxy, CDP and Node injection configuration before Chromium launch."""
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "LD_LIBRARY_PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "FONTCONFIG_PATH",
        "FONTCONFIG_FILE",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)
    env.pop("NODE_OPTIONS", None)
    env["HOME"] = home
    env["TMPDIR"] = home
    return env


def matches_destination_url(
    url: str, file_id: str, tab_id: str, *, origin: str = PRODUCTION_EDITOR_ORIGIN
) -> bool:
    requested = urlsplit(url)
    expected = urlsplit(origin)
    if (
        requested.scheme != "https"
        or requested.hostname != expected.hostname
        or requested.port != expected.port
        or requested.username is not None
        or requested.password is not None
    ):
        return False
    parts = requested.path.strip("/").split("/")
    try:
        index = parts.index("document")
        tail = parts[index + 1 :]
        if tail and tail[0].startswith("u") and len(tail) >= 3:
            tail = tail[2:]
        if len(tail) != 3 or tail[0] != "d" or tail[1] != file_id or tail[2] != "edit":
            return False
    except (ValueError, IndexError):
        return False
    query = parse_qs(requested.query, keep_blank_values=True)
    return query.get("tab") == [tab_id]


def chromium_args() -> list[str]:
    return [
        "--disable-quic",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-features=MediaRouter,OptimizationHints,GlobalMediaControls,UseDnsHttpsSvcbAlpn",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--no-first-run",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
    ]


class BrowserEngine:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        policy_factory: Callable[[], EgressPolicy] = EgressPolicy,
        editor_origin: str = PRODUCTION_EDITOR_ORIGIN,
        resource_hosts: frozenset[str] = RESOURCE_HOSTS,
    ) -> None:
        origin = urlsplit(editor_origin)
        if origin.scheme != "https" or not origin.hostname:
            raise ValueError("editor origin must be HTTPS")
        self.settings = settings
        self.policy_factory = policy_factory
        self.editor_origin = editor_origin.rstrip("/")
        self.editor_host = origin.hostname.casefold()
        self.editor_port = origin.port
        self.resource_hosts = resource_hosts | {self.editor_host}

    async def close(self) -> None:
        """Each job owns and closes its browser, context and CONNECT proxy in run()."""
        return None

    def _job_url(self, request: CreateJobRequest) -> str:
        if self.editor_origin == PRODUCTION_EDITOR_ORIGIN:
            return editor_url(request.destination_file_id, request.destination_tab_id)
        return (
            f"{self.editor_origin}/document/d/{request.destination_file_id}/edit"
            f"?tab={request.destination_tab_id}"
        )

    async def run(self, request, operation, cancel_event, before_mutation) -> str:
        if cancel_event.is_set():
            raise CancelledAtBoundary()
        try:
            storage_state = self.settings.read_storage_state()
        except RuntimeError as exc:
            raise WorkerFault("session_seed_unavailable", phase="needs_login") from exc

        policy = self.policy_factory()
        if self.editor_host not in self.resource_hosts:
            raise WorkerFault("editor_host_not_allowlisted", phase="blocked")
        policy = AllowlistedEgressPolicy(self.resource_hosts, policy)
        proxy = EgressProxy(policy)
        port = await proxy.start()
        lease = proxy.register(
            RenderBudget(
                max_bytes=self.settings.max_page_bytes,
                max_concurrent_tunnels=24,
                max_tunnels=200,
                idle_timeout_s=20,
            )
        )
        browser: Browser | None = None
        context: BrowserContext | None = None
        violation: list[str] = []
        allowed_page: list[Page | None] = [None]
        allow_new_page = [False]
        current_page: list[Page | None] = [None]
        try:
            with tempfile.TemporaryDirectory(prefix="google-browser-") as home:
                async with async_playwright() as playwright:
                    launch_args = chromium_args()
                    launch_kwargs: dict[str, object] = {
                        "headless": True,
                        "chromium_sandbox": True,
                        "args": launch_args,
                        "env": clean_browser_environment(home),
                        "proxy": {
                            "server": f"http://127.0.0.1:{port}",
                            "username": lease.token,
                            "password": "render",
                            "bypass": "",
                        },
                    }
                    if self.settings.chromium_executable_path:
                        launch_kwargs["executable_path"] = str(
                            self.settings.chromium_executable_path
                        )
                    elif self.settings.chromium_channel == "chrome":
                        launch_kwargs["channel"] = "chrome"
                    browser = await playwright.chromium.launch(**launch_kwargs)
                    ignore_cert_errors = bool(
                        policy.overrides and policy.overrides.ignore_cert_errors
                    )
                    context = await browser.new_context(
                        storage_state=storage_state,
                        accept_downloads=False,
                        service_workers="block",
                        ignore_https_errors=ignore_cert_errors,
                    )
                    page = await context.new_page()
                    allowed_page[0] = page
                    current_page[0] = page

                    def on_page(new_page: Page) -> None:
                        if allow_new_page[0]:
                            allow_new_page[0] = False
                            allowed_page[0] = new_page
                        else:
                            violation.append("popup")
                            asyncio.create_task(new_page.close())

                    def on_dialog(dialog) -> None:
                        violation.append("dialog")
                        asyncio.create_task(dialog.dismiss())

                    def on_download(_download) -> None:
                        violation.append("download")

                    context.on("page", on_page)
                    page.on("dialog", on_dialog)
                    page.on("download", on_download)

                    async def route_request(route) -> None:
                        url = route.request.url
                        parsed = urlsplit(url)
                        host = (parsed.hostname or "").casefold().rstrip(".")
                        if parsed.scheme != "https" or host not in self.resource_hosts:
                            if route.request.is_navigation_request():
                                violation.append("blocked_navigation")
                            await route.abort("blockedbyclient")
                            return
                        active_page = current_page[0]
                        if route.request.is_navigation_request():
                            try:
                                frame = route.request.frame
                            except Exception:
                                violation.append("unbound_navigation")
                                await route.abort("blockedbyclient")
                                return
                        else:
                            frame = None
                        if active_page is not None and frame == active_page.main_frame:
                            if host == "accounts.google.com":
                                violation.append("login_redirect")
                                await route.abort("blockedbyclient")
                                return
                            if not matches_destination_url(
                                url,
                                request.destination_file_id,
                                request.destination_tab_id,
                                origin=self.editor_origin,
                            ):
                                violation.append("wrong_destination")
                                await route.abort("blockedbyclient")
                                return
                        await route.continue_()

                    async def guarded_before_mutation() -> None:
                        if cancel_event.is_set():
                            raise CancelledAtBoundary()
                        if violation:
                            raise WorkerFault("browser_policy_block", phase="blocked")
                        if not matches_destination_url(
                            page.url,
                            request.destination_file_id,
                            request.destination_tab_id,
                            origin=self.editor_origin,
                        ):
                            raise WorkerFault("wrong_destination", phase="blocked")
                        await ui.check_identity()
                        await ui.check_editable()
                        await ui.check_dialog_state(
                            "Find and replace" if operation.kind == "replace_text" else None
                        )
                        await before_mutation()

                    await context.route("**/*", route_request)
                    if cancel_event.is_set():
                        raise CancelledAtBoundary()
                    url = self._job_url(request)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                    except Exception as exc:
                        if "login_redirect" in violation:
                            raise WorkerFault("needs_login", phase="needs_login") from exc
                        if violation:
                            raise WorkerFault("navigation_blocked", phase="blocked") from exc
                        raise WorkerFault("editor_unavailable", phase="needs_login") from exc
                    if violation:
                        raise WorkerFault("browser_policy_block", phase="blocked")
                    if not matches_destination_url(
                        page.url,
                        request.destination_file_id,
                        request.destination_tab_id,
                        origin=self.editor_origin,
                    ):
                        raise WorkerFault("wrong_destination", phase="blocked")

                    ui = DocsUiAdapter(page, self.settings.editor_email, request.destination_tab_id)
                    await ui.check_identity()
                    await ui.check_editable()
                    if cancel_event.is_set():
                        raise CancelledAtBoundary()
                    await ui.apply(operation, guarded_before_mutation)
                    if violation:
                        raise WorkerFault("unexpected_browser_action", phase="blocked")
                    await ui.wait_saved()
                    await page.close()

                    if cancel_event.is_set():
                        raise CancelledAtBoundary()
                    allow_new_page[0] = True
                    reopened = await context.new_page()
                    if allowed_page[0] is not reopened or violation:
                        raise WorkerFault("popup_or_extra_tab", phase="blocked")
                    current_page[0] = reopened
                    reopened.on("dialog", on_dialog)
                    reopened.on("download", on_download)
                    try:
                        await reopened.goto(url, wait_until="domcontentloaded", timeout=15_000)
                    except Exception as exc:
                        raise WorkerFault("reopen_failed", phase="blocked") from exc
                    if violation or not matches_destination_url(
                        reopened.url,
                        request.destination_file_id,
                        request.destination_tab_id,
                        origin=self.editor_origin,
                    ):
                        raise WorkerFault("reopen_wrong_destination", phase="blocked")
                    await DocsUiAdapter(
                        reopened, self.settings.editor_email, request.destination_tab_id
                    ).verify_after_reopen(operation)
                    return "awaiting_verification"
        finally:
            if context:
                with contextlib.suppress(Exception):
                    await context.close()
            if browser:
                with contextlib.suppress(Exception):
                    await browser.close()
            proxy.unregister(lease)
            await proxy.close()
