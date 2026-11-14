"""Deterministic rules that decide when the image skill is active without a router hit.

Activation only offers ``generate_image``; it never generates. With editing on, a photo the
handler would accept activates the skill whatever the text says: the model, not a word list,
decides whether the message asks for an image. Without such a photo only a narrow follow-up
to the requester's own earlier image request activates it.
"""

from types import SimpleNamespace

import pytest
from chatcore.events import Attachment, AttachmentKind, ChatRef, User, UserRef
from chatcore.testing import FakePlatform, memory_stores
from chatcore.tools.registry import RoutingContext, ToolContext, TurnState
from chatcore_image.routing import (
    is_image_request,
    pinned,
    reference_note,
    usable_references,
    wants_image,
)

PHOTO = Attachment("photo-1", AttachmentKind.PHOTO)
ALICE = "fake:alice"
BOB = "fake:bob"

# Normalized phrases from the 2026-11-14 reassessment plan; none contains a router keyword.
PLAN_CASES = [
    "generate a photo of someone climbing a tower",
    "ok now make sure u use his face when u generate the Titan",
    "generate it now!!",
    "use this face",
    "ok now use my face instead",
    "make him look like he's scaling the tower, preserve his face and body when u generate "
    "the Titan",
]
LONG_REQUEST = (
    "take the person in this photo and put them on a quiet beach at sunset with soft warm "
    "light, keep the face and clothes exactly as they are, and paint the sky in gentle "
    "orange and pink tones without adding anyone else"
)
NON_REQUESTS = [
    "",
    "thanks",
    "what is this?",
    "is this watercolour?",
    "translate this cartoon",
    "describe this anime",
    "when was this taken",
    "how much was this receipt",
    "look at my lunch",
    "这是什么卡通",
]


def _ctx(
    text="",
    *,
    attachments=(),
    owners=(),
    explicit=(),
    is_dm=False,
    reply_author="",
    reply_text="",
    reply_bot_image=False,
):
    platform = FakePlatform()
    turn = TurnState(
        attachments=tuple(attachments),
        attachment_owners=tuple(owners),
        explicit_reply_attachment_ids=frozenset(explicit),
        routing=RoutingContext(
            text=text,
            reply_author=reply_author,
            reply_text=reply_text,
            reply_bot_image=reply_bot_image,
        ),
    )
    return ToolContext(
        ChatRef(platform.name, "group"),
        User(UserRef(platform.name, "alice"), display_name="Alice"),
        is_dm,
        platform,
        memory_stores(),
        turn=turn,
    )


def _own_photo(text):
    return _ctx(text, attachments=[PHOTO], owners=[ALICE])


def test_long_request_is_longer_than_the_old_cap():
    assert len(LONG_REQUEST) > 160


@pytest.mark.parametrize("text", PLAN_CASES)
def test_plan_cases_are_not_keyword_requests(text):
    assert not is_image_request(text)


@pytest.mark.parametrize(
    "text", [*PLAN_CASES, LONG_REQUEST, "please make this photo a watercolour"]
)
def test_eligible_photo_pins_whatever_the_request_wording(text):
    assert pinned(_own_photo(text), edit_enabled=True)


@pytest.mark.parametrize("text", NON_REQUESTS)
def test_eligible_photo_pins_even_when_the_text_is_not_an_image_request(text):
    # Offering is not executing: the pin ignores the text, the model decides what to do.
    assert pinned(_own_photo(text), edit_enabled=True)


@pytest.mark.parametrize("text", [*PLAN_CASES, "make it anime", "watercolour please"])
def test_editing_off_never_pins_from_a_photo(text):
    assert not pinned(_own_photo(text), edit_enabled=False)


def test_directly_replied_photo_of_another_member_pins():
    ctx = _ctx("use this face", attachments=[PHOTO], owners=[BOB], explicit=[PHOTO.id])
    assert usable_references(ctx) == (1,)
    assert pinned(ctx, edit_enabled=True)


def test_another_members_photo_without_a_direct_reply_does_not_pin():
    ctx = _ctx("make this photo anime style please", attachments=[PHOTO], owners=[BOB])
    assert usable_references(ctx) == ()
    assert not pinned(ctx, edit_enabled=True)


def test_any_photo_in_a_dm_pins():
    ctx = _ctx("use this face", attachments=[PHOTO], owners=[BOB], is_dm=True)
    assert pinned(ctx, edit_enabled=True)


def test_non_image_attachment_does_not_pin():
    document = Attachment("doc-1", AttachmentKind.DOCUMENT, mime_type="application/pdf")
    ctx = _ctx("use this face", attachments=[document], owners=[ALICE])
    assert not pinned(ctx, edit_enabled=True)


def test_mismatched_owner_records_do_not_pin():
    ctx = _ctx("make it anime", attachments=[PHOTO], owners=[])
    assert usable_references(ctx) == ()
    assert not pinned(ctx, edit_enabled=True)


def test_only_eligible_photos_are_numbered():
    other = Attachment("photo-2", AttachmentKind.PHOTO)
    ctx = _ctx("use this face", attachments=[other, PHOTO], owners=[BOB, ALICE])
    assert usable_references(ctx) == (2,)
    assert pinned(ctx, edit_enabled=True)


@pytest.mark.parametrize("text", PLAN_CASES)
def test_plan_cases_without_a_photo_are_left_to_the_router(text):
    assert not pinned(_ctx(text), edit_enabled=True)


@pytest.mark.parametrize("edit_enabled", [True, False])
@pytest.mark.parametrize("text", ["try again", "can u do so", "iron man pls", "重试"])
def test_follow_up_to_own_prior_image_request_pins_without_a_photo(text, edit_enabled):
    ctx = _ctx(text, reply_author=ALICE, reply_text="draw a cat wearing a space helmet")
    assert pinned(ctx, edit_enabled=edit_enabled)


@pytest.mark.parametrize("text", ["thanks", "can you edit photos?", "what is this?", ""])
def test_non_follow_up_to_own_prior_image_request_does_not_pin(text):
    ctx = _ctx(text, reply_author=ALICE, reply_text="draw a cat wearing a space helmet")
    assert not pinned(ctx, edit_enabled=True)


def test_follow_up_to_another_persons_request_does_not_pin():
    ctx = _ctx("try again", reply_author=BOB, reply_text="draw a cat wearing a space helmet")
    assert not pinned(ctx, edit_enabled=True)


def test_follow_up_to_own_non_image_request_does_not_pin():
    ctx = _ctx("try again", reply_author=ALICE, reply_text="please withdraw a payment")
    assert not pinned(ctx, edit_enabled=True)


def test_reference_notes_stay_within_the_skill_context_cap():
    from chatcore.skills import MAX_SKILL_CONTEXT_CHARS

    photos = [Attachment(f"photo-{n}", AttachmentKind.PHOTO) for n in range(1, 11)]
    many = _ctx("", attachments=photos, owners=[ALICE] * 10)
    with_photos, without = reference_note(many), reference_note(_ctx(""))
    assert len(with_photos) <= MAX_SKILL_CONTEXT_CHARS
    assert len(without) <= MAX_SKILL_CONTEXT_CHARS
    assert "Usable photo reference numbers this turn: 1, 2, 3" in with_photos
    assert "A photo alone is not a request" in with_photos
    assert "Answer questions, translation requests and thanks normally." in with_photos
    assert "No usable photo is attached or directly replied to this turn" in without
    assert "never make a text-only substitute" in without
    assert "do not invent format or size errors" in without
    assert "Actual tool results are authoritative" in without


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
        "make it anime",
        "thanks",
        "can you edit photos?",
        "",
    ],
)
def test_short_phrases_without_a_prior_request_never_activate(text):
    assert not wants_image(text)


@pytest.mark.parametrize(
    "text",
    [
        "try again",
        "can u do so",
        "again pls",
        "redo",
        "重试",
        "can you do it?",
        "iron man pls",
        "@testbot iron man pls",
        "iron man, pls!",
        "watercolour please",
    ],
)
def test_retry_or_terse_polite_follow_up_to_own_prior_request_activates(text):
    assert wants_image(text, prior_image_request=True)


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
        "x" * 200,
        # Punctuation alone is not politeness.
        "the usual, like before",
        "nice lunch, right",
        "anime style?",
    ],
)
def test_other_replies_to_own_prior_request_do_not_activate(text):
    assert not wants_image(text, prior_image_request=True)


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
