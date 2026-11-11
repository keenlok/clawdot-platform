from __future__ import annotations

import pytest
from chatcore.cards.errors import CardError
from chatcore.cards.models import Card, card_json_schema, validate_card


def test_validate_card_strips_and_removes_bidi() -> None:
    card = validate_card(
        {"title": "  Hi\u202e ", "blocks": [{"type": "paragraph", "text": "a\nb"}]}
    )
    assert card.title == "Hi"
    assert card.blocks[0].text == "a\nb"


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "x", "extra": 1},
        {"title": 1},
        {
            "title": "x",
            "blocks": [{"type": "table", "columns": [{"header": "A"}], "rows": [["a", "b"]]}],
        },
        {"title": "x", "blocks": [{"type": "paragraph", "text": "bad\x00text"}]},
        {
            "title": "x",
            "blocks": [
                {"type": "sources", "items": [{"title": "x", "url": "javascript:alert(1)"}]}
            ],
        },
    ],
)
def test_invalid_card_is_safe_error(payload) -> None:
    with pytest.raises(CardError) as error:
        validate_card(payload)
    assert error.value.code == "invalid"
    assert "bad" not in str(error.value)


def test_card_text_cap_and_source_hostname() -> None:
    with pytest.raises(CardError):
        validate_card(
            {"title": "x", "blocks": [{"type": "paragraph", "text": "a" * 20}]}, max_text_chars=10
        )
    card = validate_card(
        {
            "title": "x",
            "blocks": [
                {"type": "sources", "items": [{"title": "T", "url": "https://www.example.com/a"}]}
            ],
        }
    )
    assert card.blocks[0].items[0].hostname == "example.com"


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "x", "PRIVATE_CARD_TEXT": 1},
        {"title": "x", "blocks": [{"type": "paragraph", "text": "ok", "PRIVATE_CARD_TEXT": 1}]},
    ],
)
def test_unknown_field_details_do_not_include_key(payload):
    with pytest.raises(CardError) as error:
        validate_card(payload)
    assert "PRIVATE_CARD_TEXT" not in error.value.detail


@pytest.mark.parametrize("title", ["\tTitle", "Title\n"])
def test_control_characters_are_rejected_before_stripping(title):
    with pytest.raises(CardError):
        validate_card({"title": title})
    assert validate_card({"title": "  Title  "}).title == "Title"


def _valid_card(blocks=(), **fields):
    return {"title": "x", "blocks": list(blocks), **fields}


@pytest.mark.parametrize(
    ("valid", "invalid"),
    [
        (_valid_card(title="x"), _valid_card(title="")),
        (_valid_card(title="x" * 80), _valid_card(title="x" * 81)),
        (_valid_card(subtitle="x" * 120), _valid_card(subtitle="x" * 121)),
        (
            _valid_card(blocks=[{"type": "heading", "text": "x" * 80}]),
            _valid_card(blocks=[{"type": "heading", "text": "x" * 81}]),
        ),
        (
            _valid_card(blocks=[{"type": "paragraph", "text": "x" * 1000}]),
            _valid_card(blocks=[{"type": "paragraph", "text": "x" * 1001}]),
        ),
        (
            _valid_card(blocks=[{"type": "bullets", "items": ["x"] * 12}]),
            _valid_card(blocks=[{"type": "bullets", "items": ["x"] * 13}]),
        ),
        (
            _valid_card(blocks=[{"type": "bullets", "items": ["x" * 200]}]),
            _valid_card(blocks=[{"type": "bullets", "items": ["x" * 201]}]),
        ),
        (
            _valid_card(
                blocks=[
                    {"type": "table", "columns": [{"header": "x" * 24}], "rows": [["x" * 60]] * 40}
                ]
            ),
            _valid_card(
                blocks=[{"type": "table", "columns": [{"header": "x" * 25}], "rows": [["x"]]}]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {
                        "type": "stats",
                        "tiles": [{"label": "x" * 24, "value": "x" * 18, "note": "x" * 48}],
                    }
                ]
            ),
            _valid_card(
                blocks=[
                    {"type": "stats", "tiles": [{"label": "x", "value": "x" * 19, "note": "x"}]}
                ]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {
                        "type": "stats",
                        "tiles": [{"label": "x" * 24, "value": "x" * 18, "note": "x" * 48}],
                    }
                ]
            ),
            _valid_card(
                blocks=[
                    {"type": "stats", "tiles": [{"label": "x", "value": "x", "note": "x" * 49}]}
                ]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {"type": "stats", "tiles": [{"label": "x", "value": "x"} for _ in range(4)]}
                ]
            ),
            _valid_card(
                blocks=[
                    {"type": "stats", "tiles": [{"label": "x", "value": "x"} for _ in range(5)]}
                ]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {
                        "type": "table",
                        "columns": [{"header": "x"}],
                        "rows": [["x" * 60]],
                        "total": ["x" * 60],
                    }
                ]
            ),
            _valid_card(
                blocks=[{"type": "table", "columns": [{"header": "x"}], "rows": [["x" * 61]]}]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {
                        "type": "table",
                        "columns": [{"header": "x"}],
                        "rows": [[""]],
                        "total": ["x" * 60],
                    }
                ]
            ),
            _valid_card(
                blocks=[
                    {
                        "type": "table",
                        "columns": [{"header": "x"}],
                        "rows": [[""]],
                        "total": ["x" * 61],
                    }
                ]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {
                        "type": "sources",
                        "items": [{"title": "x", "url": "https://a.test/" + "x" * 1985}],
                    }
                ]
            ),
            _valid_card(
                blocks=[
                    {
                        "type": "sources",
                        "items": [{"title": "x", "url": "https://a.test/" + "x" * 1986}],
                    }
                ]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {
                        "type": "table",
                        "columns": [{"header": "x"}] * 5,
                        "rows": [[""] * 5],
                        "total": [""] * 5,
                    }
                ]
            ),
            _valid_card(
                blocks=[{"type": "table", "columns": [{"header": "x"}] * 6, "rows": [[""] * 6]}]
            ),
        ),
        (
            _valid_card(blocks=[{"type": "callout", "tone": "info", "text": "x" * 300}]),
            _valid_card(blocks=[{"type": "callout", "tone": "info", "text": "x" * 301}]),
        ),
        (
            _valid_card(
                blocks=[
                    {
                        "type": "sources",
                        "items": [{"title": "x" * 100, "url": "https://a.test/" + "x" * 1985}],
                    }
                ]
            ),
            _valid_card(
                blocks=[
                    {"type": "sources", "items": [{"title": "x" * 101, "url": "https://a.test/"}]}
                ]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {"type": "sources", "items": [{"title": "x", "url": "https://a.test/"}] * 8}
                ]
            ),
            _valid_card(
                blocks=[
                    {"type": "sources", "items": [{"title": "x", "url": "https://a.test/"}] * 9}
                ]
            ),
        ),
        (
            _valid_card(
                blocks=[
                    {
                        "type": "stats",
                        "tiles": [{"label": "x" * 24, "value": "x" * 18, "note": "x" * 48}] * 4,
                    }
                ]
            ),
            _valid_card(
                blocks=[
                    {"type": "stats", "tiles": [{"label": "x" * 25, "value": "x", "note": "x"}]}
                ]
            ),
        ),
        (
            _valid_card(
                blocks=[{"type": "table", "columns": [{"header": "x"}], "rows": [[""]] * 40}]
            ),
            _valid_card(
                blocks=[{"type": "table", "columns": [{"header": "x"}], "rows": [[""]] * 41}]
            ),
        ),
        (
            _valid_card(blocks=[{"type": "heading", "text": "x"}] * 16),
            _valid_card(blocks=[{"type": "heading", "text": "x"}] * 17),
        ),
    ],
)
def test_model_length_and_count_boundaries(valid, invalid):
    assert validate_card(valid)
    with pytest.raises(CardError):
        validate_card(invalid)


def test_table_total_row_validates_and_renders():
    from chatcore.cards.html import render_card_html

    card = validate_card(
        {
            "title": "Totals",
            "blocks": [
                {
                    "type": "table",
                    "columns": [{"header": "Name"}, {"header": "Amount", "align": "end"}],
                    "rows": [["Alex", "2"]],
                    "total": ["Total", "2"],
                }
            ],
        }
    )
    assert '<tr class="total">' in render_card_html(card)


@pytest.mark.parametrize(
    "block",
    [
        {"type": "bullets", "items": "abc"},
        {"type": "table", "columns": [{"header": "A"}], "rows": ["x"]},
        {"type": "table", "columns": [{"header": "A"}], "rows": [["x"]], "total": "x"},
        {"type": "table", "columns": [{"header": "A"}], "rows": [[7]]},
        {"type": "table", "columns": [{"header": "A"}], "rows": [["x"]], "total": [7]},
        {"type": "stats", "tiles": {}},
    ],
)
def test_wrong_collection_types_are_card_errors(block):
    with pytest.raises(CardError) as error:
        validate_card({"title": "Bad", "blocks": [block]})
    assert error.value.code == "invalid"


@pytest.mark.parametrize(
    "source",
    [
        {"title": "x", "blocks": [{"type": "sources", "items": [{"title": "t", "url": 7}]}]},
        {"title": "x", "blocks": [{"type": "sources", "items": [{"title": "t", "url": None}]}]},
        {"title": 7},
        {"title": None},
        {"title": "x", "blocks": [{"type": "paragraph", "text": []}]},
    ],
)
def test_non_string_values_are_card_errors(source):
    with pytest.raises(CardError) as error:
        validate_card(source)
    assert error.value.code == "invalid"


def test_json_schema_has_no_refs_and_forbids_extra_properties() -> None:
    schema = card_json_schema()
    assert "$ref" not in str(schema) and "$defs" not in str(schema)
    assert schema["additionalProperties"] is False
    table = next(
        block
        for block in schema["properties"]["blocks"]["items"]["anyOf"]
        if block["properties"]["type"]["const"] == "table"
    )
    assert "exactly one cell per column" in table["properties"]["rows"]["description"]
    assert "exactly one cell per column" in table["properties"]["total"]["anyOf"][0]["description"]
    assert Card.model_validate({"title": "ok"})


def test_cards_package_exports_public_card_helpers():
    import chatcore.cards as cards

    for name in cards.__all__:
        assert getattr(cards, name)
