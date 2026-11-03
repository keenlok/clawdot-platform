"""Browse plugin tools and slash command."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from chatcore.command_prompts import CommandReply
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from chatcore_browse.deeplinks import build_search_link
from chatcore_browse.gates import build_gates
from chatcore_browse.repository import BrowseRepo
from chatcore_browse.service import BrowseActions, BrowseService
from chatcore_browse.settings import BrowseSettings

# Default behaviour text for the `rendered_pages` skill; apps may add to or override it.
RENDERED_PAGES_INSTRUCTIONS = (
    "Use `fetch_rendered_page` directly for posted or search-returned URLs when ordinary "
    "web fetch is unavailable; when it is available, use rendered fetch only after "
    "`fetch_web_page` returns empty or JavaScript-only text."
)


class BrowsePlugin(BasePlugin):
    name = "browse"
    help_title = "Web pages"
    migrations_package = "chatcore_browse.migrations"

    def skills(self):
        return [
            Skill(
                "rendered_pages",
                "Read web pages and build search links",
                (
                    "open this page",
                    "find rental car links",
                    "打开网页看看",
                    "帮我找租车",
                    "can check this site lah",
                ),
                ("fetch_rendered_page", "build_search_link"),
                keywords=("page", "browse", "网页", "搜索链接"),
                max_steps=3,
                instructions=RENDERED_PAGES_INSTRUCTIONS,
            )
        ]

    def tool_gates(self):
        return build_gates(self)

    async def on_startup(self, stores):
        self._stores = stores

    def __init__(
        self,
        settings: BrowseSettings,
        access,
        platform,
        *,
        client=None,
        clock=lambda: datetime.now(UTC),
        web_fetch_available: bool = False,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.web_fetch_available = web_fetch_available
        self.service = BrowseService(settings, access, platform, client)

    def action_handlers(self):
        actions = BrowseActions(self.service)
        return {"configure": actions} if self.settings.render_url else {}

    @staticmethod
    def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def tools(self) -> list[Tool]:
        string = {"type": "string"}

        async def build_link(ctx: ToolContext, **args: Any) -> ToolResult:
            return build_search_link(ctx, clock=self.clock, **args)

        link = Tool(
            "build_search_link",
            "Build a user-opened rental-car search link only; makes no network request.",
            self._object(
                {
                    "site": {
                        "description": "Rental provider: rentalcars or toyota_rent_a_car.",
                        "type": "string",
                        "enum": ["rentalcars", "toyota_rent_a_car"],
                    },
                    "kind": {
                        "description": "Rental search kind: rental_car.",
                        "type": "string",
                        "enum": ["rental_car"],
                    },
                    "location": {
                        **string,
                        "description": "rentalcars needs a three-letter IATA airport code; "
                        "Toyota accepts up to 120 letters, digits, spaces, or -.,'()/.",
                    },
                    "pickup_date": {**string, "description": "Pickup date (YYYY-MM-DD)."},
                    "return_date": {**string, "description": "Return date (YYYY-MM-DD)."},
                    "pickup_time": {
                        **string,
                        "description": "Local time HH:MM; minutes must be 00 or 30; default 10:00.",
                    },
                    "return_time": {
                        **string,
                        "description": "Local time HH:MM; minutes must be 00 or 30; default 10:00.",
                    },
                    "car_class": {
                        "description": "Class: economy, compact, midsize, suv, or van.",
                        "type": "string",
                        "enum": ["economy", "compact", "midsize", "suv", "van"],
                    },
                    "driver_age": {
                        "description": "Age in years; default 30, range 21–80.",
                        "type": "integer",
                        "minimum": 21,
                        "maximum": 80,
                    },
                },
                ["site", "kind", "location", "pickup_date", "return_date"],
            ),
            build_link,
        )
        if not self.settings.render_url:
            return [link]

        async def configure(ctx: ToolContext, **args: Any) -> ToolResult:
            if set(args) != {"enabled"} or type(args["enabled"]) is not bool:
                return ToolResult.error("Provide enabled as true or false.")
            try:
                action = await self.service.request_configure(ctx, args["enabled"])
                return ToolResult(content=action.summary, pending_action=action)
            except (PermissionError, ValueError, RuntimeError):
                return ToolResult.error(
                    "Only a current group manager can configure rendered fetch."
                )

        async def fetch(ctx: ToolContext, **args: Any) -> ToolResult:
            if set(args) != {"url"}:
                return ToolResult.error("Provide one URL.")
            try:
                return await self.service.fetch(ctx, args["url"])
            except Exception:
                return ToolResult.error("The page could not be rendered.")

        link.parameters["properties"]["pickup_time"]["default"] = "10:00"
        link.parameters["properties"]["return_time"]["default"] = "10:00"
        link.parameters["properties"]["driver_age"]["default"] = 30
        return [
            link,
            Tool(
                "configure_rendered_fetch",
                "Request rendered page reading on/off for this group. A current setup manager, "
                "group creator or administrator may request; requester confirms.",
                self._object(
                    {
                        "enabled": {
                            "description": "Whether to turn the feature on.",
                            "type": "boolean",
                        }
                    },
                    ["enabled"],
                ),
                configure,
                writes=True,
                admin_only=False,
            ),
            Tool(
                "fetch_rendered_page",
                (
                    "Read a posted/search-returned URL with a rendered browser. "
                    "Use after fetch_web_page returns empty or JS-only text."
                    if self.web_fetch_available
                    else "Read a posted or search-returned URL directly with a rendered browser."
                ),
                self._object(
                    {"url": {**string, "description": "Posted or search-returned HTTP(S) URL."}},
                    ["url"],
                ),
                fetch,
            ),
        ]

    def commands(self) -> list[Command]:
        if not self.settings.render_url:
            return []

        async def browsetools(ctx: CommandContext, args: str) -> str | CommandReply | None:
            value = args.strip().lower()
            if value not in {"on", "off"}:
                return CommandReply(
                    "Turn rendered page reading on or off? Reply to this message with on or off.",
                    awaits_reply=True,
                    placeholder="on or off",
                )
            try:
                action = await self.service.request_configure(ctx, value == "on")
            except (PermissionError, ValueError, RuntimeError):
                return "Only a current group manager can configure rendered fetch."
            if ctx.confirm is None:
                return "Confirmation is unavailable; nothing changed."
            await ctx.confirm.request(
                action, ctx.user, reply_to=ctx.message.ref if ctx.message else None
            )
            return None

        return [Command("browsetools", "Enable or disable rendered page reading", browsetools)]

    async def purge_chat(self, chat, stores) -> None:
        if stores.db is not None:
            await BrowseRepo(stores.db, self.settings).purge_chat(chat.key)

    async def purge_user(self, chat, user, stores) -> None:
        if stores.db is not None:
            await BrowseRepo(stores.db, self.settings).purge_user(chat.key, user.key)
