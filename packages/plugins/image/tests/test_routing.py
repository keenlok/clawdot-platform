"""Deterministic rules that decide when a short follow-up activates the image skill.

Activation only offers ``generate_image``; it never generates. The rules are intentionally
narrow: a short phrase counts only with validated context (a usable photo, a replied-to
generated photo, or the requester's own replied-to image request).
"""

import pytest
from chatcore_image.routing import is_image_request, wants_image

CONTEXTS = {
    "none": {},
    "photo": {"has_reference": True},
    "output": {"has_reference": True, "reply_is_generated_photo": True},
    "prior": {"prior_image_request": True},
}


def _wants(text, context):
    return wants_image(text, **CONTEXTS[context])


@pytest.mark.parametrize(
    "text",
    [
        "try again",
        "Try again!",
        "@testbot try again pls",
        "iron man pls",
        "can u do so",
        "can you do it?",
        "redo",
        "one more time",
        "再试一次",
        "watercolour please",
        "thanks",
        "can you edit photos?",
        "",
    ],
)
def test_short_phrases_without_context_never_activate(text):
    assert not _wants(text, "none")


@pytest.mark.parametrize(
    "text",
    [
        "iron man pls",
        "@testbot iron man pls",
        "iron man, pls!",
        "请画成钢铁侠",
        "watercolour please",
        "make it anime",
        "make it anime?",
        "do it in anime style",
        "turn him into a cartoon",
        "can you make this a cartoon?",
        "can u make this a cartoon",
        "could you please turn this into a watercolour?",
        "would you edit this photo to look like a sketch?",
        "anime style",
        "变成卡通",
        "把这个变成卡通",
        "能把这个变成卡通吗？",
    ],
)
def test_action_requests_with_a_usable_photo_activate(text):
    assert _wants(text, "photo")


@pytest.mark.parametrize(
    "text",
    [
        "thanks",
        "thank you!",
        "nice one",
        "lol",
        "谢谢",
        "can you edit photos?",
        "do you support image editing?",
        "what is this?",
        "look at my lunch",
        "translate pls",
        "read this please",
        "how much was this receipt",
        "x" * 200,
        # Punctuation alone is not politeness.
        "the usual, like before",
        "nice lunch, right",
        "look, my lunch!",
        # Questions and other tasks that merely mention a style or the photo.
        "is this watercolour?",
        "is this a watercolour",
        "what cartoon is this?",
        "which anime is this from",
        "describe this anime",
        "translate this cartoon",
        "please explain this anime style",
        "summarise this manga page pls",
        "who painted this painting?",
        "read this cartoon please",
        "can you explain how to make this a cartoon?",
        "can you make cartoons?",
        "can you do watercolour?",
        "does this filter look good?",
        "anime style?",
        "这是水彩吗？",
        "这是什么卡通",
        "翻译这个卡通",
        "描述一下这张动漫",
    ],
)
def test_photo_without_an_image_action_does_not_activate(text):
    assert not _wants(text, "photo")


@pytest.mark.parametrize("text", ["try again", "can u do so", "again pls", "redo", "重试"])
def test_retry_needs_a_generated_photo_reply_or_own_prior_request(text):
    assert not _wants(text, "photo")  # a plain photo is not a prior image request
    assert _wants(text, "output")
    assert _wants(text, "prior")


def test_terse_subject_follow_up_to_own_prior_request_activates():
    assert _wants("iron man pls", "prior")
    assert not _wants("thanks", "prior")
    assert not _wants("can you edit photos?", "prior")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("draw a cat wearing a space helmet", True),
        ("@bot Make this photo a watercolour", True),
        ("帮我画一只狗", True),
        ("Draw me like one of your sketches", True),
        ("a picture of a sunset, please", True),
        ("illustrate the idea", True),
        ("把这张照片改成水彩", True),
        ("生成图片：一只猫", True),
        ("try again", False),
        ("iron man pls", False),
        ("what's the weather", False),
        ("", False),
        # Keywords match on word boundaries, exactly as the router does.
        ("please withdraw a payment", False),
        ("withdraw an offer", False),
        ("overdraw my account", False),
        ("redraw me the chart", False),
        ("draw another card", False),
        ("the image often fails", False),
        ("a motion picture often bores me", False),
        ("that was well illustrated", False),
        ("unillustrate", False),
    ],
)
def test_prior_request_detection_uses_only_the_declared_keywords(text, expected):
    assert is_image_request(text) is expected


ROUTER_PARITY = [
    "draw a cat",
    "DRAW AN owl",
    "please withdraw a payment",
    "overdraw my account",
    "draw another card",
    "illustration",
    "illustrations",
    "an illustration, please",
    "帮我画一只狗",
    "x画一x",
    "make this photo, a watercolour",
    "remake this photo",
    "picture of",
    "picture offer",
    "",
]


@pytest.mark.parametrize("text", ROUTER_PARITY)
def test_prior_request_detection_matches_the_routers_keyword_boundaries(text):
    from types import SimpleNamespace

    from chatcore.skill_router import SkillRouter
    from chatcore_image.routing import KEYWORDS

    skills = SimpleNamespace(
        names=lambda: ["image_generation"], get=lambda name: SimpleNamespace(keywords=KEYWORDS)
    )
    router = SkillRouter(skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05)
    assert is_image_request(text) is bool(router._keyword_hits(text))


def test_contextual_phrases_are_not_global_router_keywords():
    from chatcore_image.routing import FOLLOW_UPS, KEYWORDS

    assert not set(FOLLOW_UPS) & set(KEYWORDS)
    for phrase in ("try again", "iron man", "can u do so", "pls", "please", "again"):
        assert not any(phrase in keyword or keyword in phrase for keyword in KEYWORDS)
