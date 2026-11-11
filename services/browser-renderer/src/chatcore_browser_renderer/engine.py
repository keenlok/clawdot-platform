from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from chatcore.security.egress_proxy import EgressPolicy, EgressProxy, PolicyOverrides, RenderBudget
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .blocking import detect_block
from .settings import RendererSettings

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
ALLOWED_SCHEMES = {"https", "data", "blob", "about"}
BLOCKED_TYPES = {"media", "font", "image", "websocket", "eventsource"}
_MIN_PAGE_CSS_PX = 80


def browser_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Keep browser discovery while removing inherited proxy and Playwright overrides."""
    environment = dict(os.environ if source is None else source)
    return {
        key: value
        for key, value in environment.items()
        if (
            key == "PLAYWRIGHT_BROWSERS_PATH"
            or not key.upper().startswith("PLAYWRIGHT_")
            and key.upper() != "NODE_OPTIONS"
            and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        )
    }


@dataclass(slots=True)
class RenderResult:
    status: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "final_url": self.final_url,
            "title": self.title,
            "text": self.text,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class CardRenderResult:
    status: str
    pages: list[bytes]


class RendererEngine:
    def __init__(
        self,
        settings: RendererSettings | None = None,
        *,
        proxy: EgressProxy | None = None,
        browser: Any | None = None,
        browser_factory: Any | None = None,
        overrides: PolicyOverrides | None = None,
    ) -> None:
        self.settings = settings or RendererSettings()
        self.proxy = proxy or EgressProxy(EgressPolicy(overrides=overrides))
        self.browser = browser
        self.browser_factory = browser_factory
        self.browser_manager: Any | None = None
        self.port: int | None = None
        self.active = 0
        self.render_count = 0
        self._browser_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._recycle_pending = False
        self.last_refused_authorities: tuple[str, ...] = ()
        self.last_connect_refused_authorities: tuple[str, ...] = ()
        self.last_route_refused_authorities: tuple[str, ...] = ()
        self._active_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        self.port = await self.proxy.start()
        await self._ensure_browser()

    async def close(self) -> None:
        for task in tuple(self._active_tasks):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        async with self._state_lock:
            async with self._browser_lock:
                await self._close_browser_locked()
        await self.proxy.close()

    async def _ensure_browser(self) -> None:
        async with self._browser_lock:
            await self._ensure_browser_locked()

    async def _ensure_browser_locked(self) -> None:
        if self.browser is not None:
            return
        if self.browser_factory is not None:
            self.browser = await self.browser_factory()
            return
        from playwright.async_api import async_playwright

        self.browser_manager = await async_playwright().start()
        args = [
            # Send all web TCP connections to the per-process guarded CONNECT proxy.
            f"--proxy-server=http://127.0.0.1:{self.port}",
            # Do not let Chromium bypass the proxy for loopback destinations.
            "--proxy-bypass-list=<-loopback>",
            # Keep Chromium from resolving destinations independently; only the proxy resolves.
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            # Prevent UDP-based HTTP/3 from avoiding the TCP proxy.
            "--disable-quic",
            # Prevent WebRTC from sending UDP outside the proxy.
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            # Stop background connections unrelated to the requested page.
            "--disable-background-networking",
            # Prevent Chromium account and synchronization traffic.
            "--disable-sync",
            # Disable extension background activity.
            "--disable-extensions",
            # Avoid first-run tabs and setup activity.
            "--no-first-run",
            # Prevent page audio playback.
            "--mute-audio",
            # A text-only renderer needs no GPU; headless shell's GPU process crashes on macOS.
            "--disable-gpu",
            # Bound the JavaScript heap for the renderer process.
            "--js-flags=--max-old-space-size=256",
        ]
        # Each flag disables a non-proxy egress path or unattended Chromium activity; no-sandbox
        # is an explicit deployment exception whose other controls are documented in compose.
        launch: dict[str, Any] = {
            "args": args,
            "headless": True,
            "chromium_sandbox": not self.settings.browser_no_sandbox,
            "env": browser_environment(),
        }
        self.browser = await self.browser_manager.chromium.launch(**launch)

    async def _close_browser(self) -> None:
        async with self._browser_lock:
            await self._close_browser_locked()

    async def _close_browser_locked(self) -> None:
        browser, self.browser = self.browser, None
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        manager, self.browser_manager = self.browser_manager, None
        if manager is not None:
            await manager.stop()

    async def render(self, url: str) -> RenderResult:
        current = asyncio.current_task()
        async with self._state_lock:
            if self.active >= self.settings.render_concurrency:
                return RenderResult("busy")
            if self.active == 0 and self._recycle_pending:
                async with self._browser_lock:
                    await self._close_browser_locked()
                self._recycle_pending = False
                self.render_count = 0
            self.active += 1
            if current:
                self._active_tasks.add(current)
        try:
            try:
                await self._ensure_browser()
            except Exception:
                self._recycle_pending = True
            if self.browser is None:
                return RenderResult("error")
            return await self._render(url)
        except Exception:
            self._recycle_pending = True
            return RenderResult("error")
        finally:
            async with self._state_lock:
                self.active -= 1
                if current:
                    self._active_tasks.discard(current)
                if self.active == 0 and self._recycle_pending:
                    async with self._browser_lock:
                        await self._close_browser_locked()
                    self._recycle_pending = False
                    self.render_count = 0

    async def render_card(self, html: str, theme: str) -> CardRenderResult:
        current = asyncio.current_task()
        async with self._state_lock:
            if self.active >= self.settings.render_concurrency:
                return CardRenderResult("busy", [])
            if self.active == 0 and self._recycle_pending:
                async with self._browser_lock:
                    await self._close_browser_locked()
                self._recycle_pending = False
                self.render_count = 0
            self.active += 1
            if current:
                self._active_tasks.add(current)
        context = None
        try:
            async with asyncio.timeout(self.settings.card_timeout_s):
                await self._ensure_browser()
                if self.browser is None:
                    return CardRenderResult("error", [])
                context = await self.browser.new_context(
                    java_script_enabled=False,
                    offline=True,
                    accept_downloads=False,
                    service_workers="block",
                    permissions=[],
                    viewport={"width": 400, "height": 600},
                    device_scale_factor=2.5,
                    color_scheme=theme,
                )

                async def abort(route, _request=None):
                    await route.abort()

                await context.route("**/*", abort)
                page = await context.new_page()
                await page.set_content(html, wait_until="load")
                height = int(
                    await page.locator("body").evaluate(
                        "element => Math.ceil(element.scrollHeight)"
                    )
                )
                if height <= 0:
                    return CardRenderResult("error", [])
                max_height = max(_MIN_PAGE_CSS_PX, self.settings.card_page_css_px)
                boundaries = []
                try:
                    for selector in ("[data-block]", "tr[data-row]"):
                        locator = page.locator(selector)
                        for index in range(await locator.count()):
                            box = await locator.nth(index).bounding_box()
                            if box:
                                boundaries.append(
                                    (float(box["y"]), float(box["y"] + box["height"]))
                                )
                    boundaries = sorted(set(boundaries))
                except Exception:
                    boundaries = []
                tops = []
                top = 0
                while top < height:
                    remaining = height - top
                    if remaining <= max_height or remaining < 2 * _MIN_PAGE_CSS_PX:
                        tops.append((top, remaining))
                        break
                    limit = min(height, top + max_height)
                    candidates = [
                        end for _start, end in boundaries if top + _MIN_PAGE_CSS_PX <= end <= limit
                    ]
                    end = max(candidates) if candidates else limit
                    remainder = height - end
                    if 0 < remainder < _MIN_PAGE_CSS_PX:
                        eligible = [
                            boundary
                            for _start, boundary in boundaries
                            if top + _MIN_PAGE_CSS_PX <= boundary < end
                            and height - boundary >= _MIN_PAGE_CSS_PX
                        ]
                        if eligible:
                            end = max(eligible)
                        else:
                            end -= _MIN_PAGE_CSS_PX - remainder
                    tops.append((top, end - top))
                    top = end
                if len(tops) > self.settings.card_max_pages:
                    return CardRenderResult("too_large", [])
                pages = []
                for top, page_height in tops:
                    png = await page.screenshot(
                        type="png",
                        clip={"x": 0, "y": top, "width": 400, "height": page_height},
                        full_page=True,
                    )
                    if len(png) > self.settings.card_max_png_bytes:
                        return CardRenderResult("too_large", [])
                    pages.append(png)
                return CardRenderResult("ok", pages)
        except TimeoutError:
            return CardRenderResult("timeout", [])
        except Exception:
            self._recycle_pending = True
            return CardRenderResult("error", [])
        finally:
            if context is not None:
                try:
                    async with asyncio.timeout(2):
                        await context.close()
                except Exception:
                    self._recycle_pending = True
            self.render_count += 1
            if self.render_count >= self.settings.render_max_renders_per_browser:
                self._recycle_pending = True
            async with self._state_lock:
                self.active -= 1
                if current:
                    self._active_tasks.discard(current)
                if self.active == 0 and self._recycle_pending:
                    async with self._browser_lock:
                        await self._close_browser_locked()
                    self._recycle_pending = False
                    self.render_count = 0

    async def _render(self, url: str) -> RenderResult:
        budget = RenderBudget(max_bytes=self.settings.render_max_bytes)
        lease = self.proxy.register(budget)
        context = None
        navigation_hosts: set[str] = set()
        try:
            async with asyncio.timeout(self.settings.render_timeout_s):
                context = await self.browser.new_context(
                    accept_downloads=False,
                    service_workers="block",
                    permissions=[],
                    java_script_enabled=True,
                    locale="en-US",
                    viewport={"width": 1280, "height": 800},
                    ignore_https_errors=bool(
                        self.proxy.policy.overrides
                        and self.proxy.policy.overrides.ignore_cert_errors
                    ),
                    proxy={
                        "server": f"http://127.0.0.1:{self.port}",
                        "username": lease.token,
                        "password": "render",
                        "bypass": "<-loopback>",
                    },
                )

                async def route_request(route: Any, request: Any | None = None) -> None:
                    await self._route(route, request, budget)

                await context.route("**/*", route_request)
                page = await context.new_page()
                page.on("dialog", self._dismiss_dialog)
                page.on("popup", self._close_popup)
                challenge_urls: list[str] = []

                def collect_page_request(request: Any) -> None:
                    try:
                        is_navigation = request.is_navigation_request()
                        if is_navigation and request.frame == page.main_frame:
                            redirected = request
                            while redirected is not None:
                                hostname = self._normalized_hostname(redirected.url)
                                if hostname:
                                    navigation_hosts.add(hostname)
                                redirected = redirected.redirected_from
                        elif (
                            request.frame != page.main_frame and request.resource_type == "document"
                        ):
                            challenge_urls.append(request.url)
                    except Exception:
                        return

                page.on("request", collect_page_request)
                response = await page.goto(url, wait_until="domcontentloaded")
                status_code = response.status if response else 200
                with contextlib.suppress(PlaywrightTimeoutError):
                    await page.wait_for_load_state(
                        "networkidle", timeout=self.settings.render_network_idle_s * 1000
                    )
                if self.settings.render_settle_s:
                    await asyncio.sleep(self.settings.render_settle_s)
                title = await page.title()
                text = await page.inner_text("body")
                main_frame = getattr(page, "main_frame", None)
                frames = [
                    frame.url
                    for frame in page.frames
                    if frame is not main_frame
                    and (main_frame is not None or frame is not page.frames[0])
                ]
                frames.extend(challenge_urls)
                if budget.too_large:
                    return RenderResult("too_large")
                if detect_block(status_code, title, text, frames):
                    return RenderResult("blocked_by_site")
                if status_code < 200 or status_code >= 300:
                    return RenderResult("error")
                cleaned_title = self._clean(title, 500)
                normalized_text = " ".join(CONTROL_CHARS.sub("", text).split())
                cleaned_text = normalized_text[: self.settings.render_max_chars]
                return RenderResult(
                    "ok",
                    final_url=page.url,
                    title=cleaned_title,
                    text=cleaned_text,
                    truncated=len(normalized_text) > self.settings.render_max_chars,
                )
        except TimeoutError:
            return RenderResult("timeout")
        except Exception:
            if budget.too_large:
                return RenderResult("too_large")
            if navigation_hosts.intersection(budget.connect_refused_authorities):
                return RenderResult("blocked_url")
            self._recycle_pending = True
            return RenderResult("error")
        finally:
            self.last_refused_authorities = tuple(budget.refused_authorities)
            self.last_connect_refused_authorities = tuple(budget.connect_refused_authorities)
            self.last_route_refused_authorities = tuple(budget.route_refused_authorities)
            self.proxy.unregister(lease)
            if context is not None:
                try:
                    async with asyncio.timeout(2):
                        await context.close()
                except Exception:
                    self._recycle_pending = True
            self.render_count += 1
            if self.render_count >= self.settings.render_max_renders_per_browser:
                self._recycle_pending = True

    @staticmethod
    def _clean(value: str, limit: int) -> str:
        return " ".join(CONTROL_CHARS.sub("", value).split())[:limit]

    @staticmethod
    def _normalized_hostname(value: str) -> str:
        raw_hostname = urlsplit(value).hostname or ""
        try:
            return raw_hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        except UnicodeError:
            return raw_hostname.casefold()

    @staticmethod
    async def _route(
        route: Any, request: Any | None = None, budget: RenderBudget | None = None
    ) -> None:
        req = request or route.request
        parsed = urlsplit(req.url)
        scheme = parsed.scheme.lower()
        if (
            scheme not in ALLOWED_SCHEMES
            or parsed.username is not None
            or parsed.password is not None
            or req.resource_type in BLOCKED_TYPES
        ):
            if budget is not None and parsed.netloc:
                budget.refuse(parsed.netloc, source="route")
            await route.abort()
        else:
            await route.continue_()

    @staticmethod
    async def _dismiss_dialog(dialog: Any) -> None:
        await dialog.dismiss()

    @staticmethod
    async def _close_popup(page: Any) -> None:
        await page.close()
