"""When a short follow-up should make the image skill active without a router hit.

These rules only decide whether ``generate_image`` is offered this turn. They never authorize
or run a generation: the model still needs an explicit request, and the tool handler re-checks
access and photo provenance. They are deliberately narrow. A short phrase counts only together
with context that core validated for this requester and topic: a usable photo, a directly
replied-to generated photo, or the requester's own replied-to image request. None of these
phrases is a router keyword, so without that context they activate nothing.
"""

from __future__ import annotations

import re

from chatcore.images import is_image
from chatcore.skills import keyword_matches
from chatcore.tools.registry import ToolContext

from chatcore_image.permissions import may_use_reference

# Literal router hints for the skill declaration; also how a replied-to message is recognized
# as an earlier image request.
KEYWORDS: tuple[str, ...] = (
    "draw a",
    "draw an",
    "draw me",
    "draw my",
    "picture of",
    "image of",
    "generate an image",
    "make an image",
    "make this photo",
    "make this picture",
    "turn this photo",
    "turn this into",
    "edit this photo",
    "this photo into",
    "illustrate",
    "illustration",
    "画一",
    "画个",
    "画张",
    "帮我画",
    "生成图片",
    "把这张",
    "照片改成",
    "照片变成",
    "图片改成",
)

# Whole-message follow-ups (after dropping the bot mention, politeness and punctuation).
FOLLOW_UPS: tuple[str, ...] = (
    "try again",
    "again",
    "retry",
    "redo",
    "redo it",
    "do it again",
    "one more time",
    "once more",
    "another one",
    "can u do so",
    "can you do so",
    "can u do it",
    "can you do it",
    "do so",
    "do it",
    "go ahead",
    "再来一次",
    "再试一次",
    "再试",
    "重试",
    "再来",
)

_MAX_CHARS = 160
_TERSE_WORDS = 4
_POLITE = ("please", "pls", "plz", "请", "拜托")
_ACKS = frozenset(
    {"thanks", "thank you", "thx", "ty", "ok", "okay", "nice", "nice one", "cool", "great"}
    | {"lol", "haha", "wow", "love it", "谢谢", "好的", "好"}
)
_MENTION = re.compile(r"@\w+")
_EDGE = re.compile(r"^[\s,.!?。！？，~]+|[\s,.!?。！？，~]+$")
# "do it in anime style" is an instruction; "do you ..." is a question.
_QUESTION_START = re.compile(
    r"^(?:(?:can|could|does|did|are|is|will|would|should|what|how|why|when|who|where|which)\b"
    r"|do (?:you|u|we|i|they)\b|能|可以|可不可以)"
)
# A question that is really a polite instruction: "can you make this a cartoon?".
_REQUEST_START = re.compile(
    r"^(?:(?:can|could|will|would) (?:you|u)\b|能不能|可不可以|可以|能否|能)"
)
_TARGET = re.compile(
    r"\b(this|that|it|these|those|him|her|me|us|them|photo|picture|image|pic|background)\b"
    r"|这|那|它|他|她|我"
)
_EDIT_VERB = re.compile(
    r"\b(make|turn|convert|edit|restyle|redraw|recolou?r|transform|cartoonify|photoshop)\b"
    r"|改成|变成|换成|修图"
)
_EDIT_START = re.compile(
    r"^(?:make|turn|convert|edit|restyle|redraw|recolou?r|transform|cartoonify|photoshop)\b"
)
_STYLE = re.compile(
    r"\b(cartoon|anime|manga|watercolou?r|sketch|painting|pixel art|oil paint|style|filter)\b"
    r"|卡通|动漫|水彩|素描|风格|滤镜"
)
# Requests about a photo that are not image generation, even when they name a style: reading,
# describing, translating, bookkeeping and information questions.
_OTHER_TASK = re.compile(
    r"\b(read|translate|summari[sz]e|summary|describe|explain|identify|recogni[sz]e|tell|scan"
    r"|ocr|save|record|log|track|remind|split|receipt|expense|bill|what|who|where|when|why|how"
    r"|which)\b"
    r"|翻译|总结|解释|描述|识别|记账|账单|什么|怎么|为什么|哪"
)


def _normalize(text: str) -> str:
    cleaned = _MENTION.sub(" ", text.casefold())
    return _EDGE.sub("", " ".join(cleaned.split()))


def _without_politeness(text: str) -> tuple[str, bool]:
    """Drop politeness words; also say whether one was present.

    Punctuation is stripped too, so the flag, not a text comparison, tells "iron man pls" from
    a short remark that merely contains a comma.
    """
    words = [word.strip(",.!?。！？，~") for word in text.split()]
    kept = [word for word in words if word and word not in _POLITE]
    polite = len(kept) != len([word for word in words if word])
    joined = " ".join(kept)
    for marker in ("请", "拜托"):
        polite = polite or marker in joined
        joined = joined.replace(marker, "")
    return joined.strip(), polite


def is_image_request(text: str) -> bool:
    """True when ``text`` contains one of the skill's declared literal keywords.

    Uses the router's own matcher and its 500-character window, so this agrees with whether
    the keyword router would have routed that message to the image skill.
    """
    folded = text[:500].casefold()
    return any(keyword_matches(keyword, folded) for keyword in KEYWORDS)


def wants_image(
    text: str,
    *,
    has_reference: bool = False,
    reply_is_generated_photo: bool = False,
    prior_image_request: bool = False,
) -> bool:
    """Whether the current message is a contextual image request worth offering the tool for.

    ``has_reference``: a photo the requester may edit is attached or directly replied to.
    ``reply_is_generated_photo``: that photo is a recorded bot output the person replied to.
    ``prior_image_request``: the message replies to the requester's own earlier image request.
    """
    if len(text) > _MAX_CHARS or not (has_reference or prior_image_request):
        return False
    normalized = _normalize(text)
    bare, polite = _without_politeness(normalized)
    question = text.strip().endswith(("?", "？")) or bare.endswith(("吗", "么"))
    if not bare or bare in _ACKS or bare.startswith(("thank", "谢谢")):
        return False
    if bare in FOLLOW_UPS:
        return prior_image_request or (has_reference and reply_is_generated_photo)
    # Reading, describing, translating and information questions are never image actions,
    # even when they name a style ("describe this anime", "what cartoon is this?").
    if _OTHER_TASK.search(bare):
        return False
    edit = bool(_EDIT_VERB.search(bare) and _TARGET.search(bare))
    if question or _QUESTION_START.match(bare):
        # Only a polite instruction to change the photo counts ("can you make this a
        # cartoon?", "make it anime?"); "is this watercolour?" and "can you edit photos?" are
        # questions to answer.
        instruction = _REQUEST_START.match(bare) or _EDIT_START.match(bare)
        return bool(has_reference and edit and instruction)
    if has_reference and (edit or _STYLE.search(bare)):
        return True
    terse = len(bare.split()) <= _TERSE_WORDS and len(bare) <= 40
    return polite and terse


def usable_references(ctx: ToolContext) -> tuple[int, ...]:
    """1-based numbers of this turn's photos that the handler would accept as a reference."""
    try:
        owned = [
            (item, owner)
            for item, owner in zip(ctx.turn.attachments, ctx.turn.attachment_owners, strict=True)
            if is_image(item)
        ]
    except ValueError:
        return ()
    return tuple(
        index
        for index, (item, owner) in enumerate(owned, start=1)
        if may_use_reference(ctx, item, owner)
    )


def pinned(ctx: ToolContext, *, edit_enabled: bool) -> bool:
    """Skill pin: activate for a contextual follow-up backed by validated context."""
    routing = ctx.turn.routing
    has_reference = edit_enabled and bool(usable_references(ctx))
    prior = bool(
        routing.reply_author
        and routing.reply_author == ctx.user.ref.key
        and is_image_request(routing.reply_text)
    )
    return wants_image(
        routing.text,
        has_reference=has_reference,
        reply_is_generated_photo=routing.reply_bot_image,
        prior_image_request=prior,
    )


def reference_note(ctx: ToolContext) -> str:
    """Trusted per-turn note, built only from validated reference numbers."""
    numbers = usable_references(ctx)
    if not numbers:
        return (
            "No usable photo is attached or directly replied to this turn, so do not pass "
            "reference. If the person wants a photo changed, ask them to send it or reply "
            "directly to it; never make a text-only substitute."
        )
    listed = ", ".join(str(number) for number in numbers)
    return (
        f"Usable photo reference numbers this turn: {listed}. A photo alone is not a request: "
        "generate only if this message asks for a picture or a change. If a short request is "
        "unclear, ask one brief question instead of guessing."
    )
