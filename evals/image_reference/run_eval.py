"""Capture photo-edit requests from current code; score whether a live model passes reference.

Regression eval for the 2026-11-12 sb_bot report: in a group, a member sent their own photo
with "make this photo a watercolour" (as a caption, or as a reply to the photo), and the model
called ``generate_image`` without ``reference``. Each case captures the first model request of a
real agent turn (skills on, keyword router, native vision), then replays it against the
configured model and counts the tool calls.

The capture uses APIs available on v0.12.0, so this file can be copied to a v0.12.0 checkout for
a before-change baseline.

Run with the bot's model configuration in the environment:
  LLM_API_KEY=... LLM_BASE_URL=... LLM_MODEL=... \
    uv run python evals/image_reference/run_eval.py --runs 20 [--photo path/to/photo.jpg]

Only aggregate counts are printed. Prompts, photos and model replies are never written or printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import struct
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.events import Attachment, AttachmentKind, ChatRef, MessageRef, User, UserRef
from chatcore.images import sniff_image_type
from chatcore.limits import Limits
from chatcore.llm.factory import LLMSettings, build_provider
from chatcore.llm.types import Completion, Message
from chatcore.skill_turn import SkillSettings
from chatcore.skills import SkillsMode
from chatcore.testing import FakePlatform, ScriptedProvider, memory_stores, message
from chatcore_image import ImagePlugin, ImageSettings
from pydantic import SecretStr

GROUP = ChatRef("tg", "-1001111111111")
MEMBER_A = User(UserRef("tg", "111111111"), "Member A", "member_a")
BOT_NAME = "clawdot"
REQUEST = f"@{BOT_NAME} make this photo a watercolour"


def _sample_png(size: int = 64) -> bytes:
    """A small, valid RGB PNG: a sky-blue top half over a green bottom half."""
    rows = b"".join(
        b"\x00" + (b"\x87\xce\xeb" if y < size // 2 else b"\x22\x8b\x22") * size
        for y in range(size)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


async def _capture(photo: bytes, *, as_reply: bool) -> dict:
    stores = memory_stores()
    platform = FakePlatform(name="tg")
    platform.files["photo-1"] = photo
    access = AccessPolicy([GROUP.key], [])
    plugin = ImagePlugin(
        access,
        platform,
        settings=ImageSettings(
            IMAGE_ENABLED=True, IMAGE_EDIT_ENABLED=True, IMAGE_OPENAI_API_KEY=SecretStr("unused")
        ),
    )
    scripted = ScriptedProvider([Completion(Message.assistant("Fixture reply."), "end_turn")])
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[ConversationPlugin(), plugin],
        bot_name=BOT_NAME,
    )
    app.responder = AgentResponder(
        scripted,
        system_prompt="You are a helpful group chat assistant.",
        settings=AgentSettings(
            skills=SkillSettings(
                mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"
            ),
            vision=True,
        ),
        limits=Limits(stores.cache),
        clock=lambda: datetime.now(UTC),
    )
    attachment = Attachment("photo-1", AttachmentKind.PHOTO)
    if as_reply:
        sent = message("", sender=MEMBER_A, chat=GROUP, attachments=[attachment])
        await app.handle(sent)
        current = replace(
            message(REQUEST, sender=MEMBER_A, chat=GROUP, mention=True),
            reply_to=MessageRef(GROUP, sent.message_id),
        )
    else:
        current = message(
            REQUEST, sender=MEMBER_A, chat=GROUP, mention=True, attachments=[attachment]
        )
    await app.handle(current)
    if not scripted.calls:
        raise SystemExit("capture failed: the agent made no model request")
    return scripted.calls[0]


async def capture_requests(photo: bytes) -> dict[str, dict]:
    return {
        "own-photo-caption": await _capture(photo, as_reply=False),
        "reply-to-own-photo": await _capture(photo, as_reply=True),
    }


def classify(completion: Completion) -> str:
    """Return ``reference``, ``no_reference`` or ``no_call`` for the first image tool call."""
    for call in completion.message.tool_calls:
        if call.name == "generate_image":
            return "reference" if call.arguments.get("reference") == 1 else "no_reference"
    return "no_call"


async def run(runs: int, model: str, photo: bytes) -> None:
    settings = LLMSettings(
        LLM_PROVIDER=os.environ.get("LLM_PROVIDER", "openai_compat"),
        LLM_BASE_URL=os.environ.get("LLM_BASE_URL"),
        LLM_MODEL=model,
        LLM_API_KEY=os.environ.get("LLM_API_KEY"),
    )
    provider = build_provider(settings)
    requests = await capture_requests(photo)
    total_errors = 0
    for case, captured in requests.items():
        counts = {"reference": 0, "no_reference": 0, "no_call": 0}
        errors = 0
        for index in range(runs):
            try:
                completion = await provider.complete(
                    captured["messages"],
                    system=captured["system"],
                    tools=captured["tools"],
                    tool_choice=captured["tool_choice"],
                    max_tokens=captured["max_tokens"],
                    session_key=f"image-reference:{case}:{index}",
                )
                counts[classify(completion)] += 1
            except Exception:  # noqa: BLE001 - report only an aggregate provider error count
                errors += 1
        total_errors += errors
        scored = runs - errors
        rate = f"{counts['reference'] / scored:.0%}" if scored else "n/a"
        print(
            f"{case}: reference={counts['reference']}/{scored} rate={rate} "
            f"no_reference={counts['no_reference']} no_call={counts['no_call']} errors={errors}"
        )
    if total_errors:
        raise SystemExit(
            f"{total_errors} provider call(s) failed; rates exclude them and are not a valid result"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    parser.add_argument(
        "--photo",
        type=Path,
        help="JPEG, PNG or WebP to send as the member's photo (default: a small generated PNG)",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if not args.model:
        parser.error("--model or LLM_MODEL is required")
    photo = args.photo.read_bytes() if args.photo else _sample_png()
    if sniff_image_type(photo) is None:
        parser.error("--photo must be a JPEG, PNG or WebP file")
    asyncio.run(run(args.runs, args.model, photo))


if __name__ == "__main__":
    main()
