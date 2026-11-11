from html.parser import HTMLParser

import pytest
from chatcore.cards.errors import CardError
from chatcore.cards.html import THEMES, contrast_ratio, render_card_html
from chatcore.cards.models import validate_card


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_render_escapes_values_and_uses_fixed_markup():
    card = validate_card(
        {
            "title": "</style><b>x",
            "blocks": [{"type": "paragraph", "text": "<script>alert(1)</script>"}],
        }
    )
    output = render_card_html(card)
    assert "&lt;script&gt;" in output and "<script>" not in output
    parser = Tags()
    parser.feed(output)
    assert {tag for tag, _ in parser.tags} <= {
        "html",
        "head",
        "meta",
        "style",
        "body",
        "main",
        "header",
        "div",
        "section",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "ul",
        "li",
        "span",
        "br",
    }
    assert "default-src &#x27;none&#x27;" in output or "default-src 'none'" in output
    assert "url(" not in output and "@import" not in output


def test_color_contrast_tokens():
    assert contrast_ratio("#000000", "#ffffff") == 21
    for theme in THEMES.values():
        for ink in (theme["ink"], theme["ink-2"]):
            for surface in (theme["surface"], theme["raised"]):
                assert contrast_ratio(ink, surface) >= 4.5
        for role in ("good", "warning", "critical", "info", "rule-strong"):
            assert contrast_ratio(theme[role], theme["surface"]) >= 3


@pytest.mark.parametrize(
    "injection",
    [
        "<script>alert(1)</script>",
        "</style><b>x",
        '"><img src=x onerror=alert(1)>',
        "javascript:alert(1)",
        "url(https://example.com/x)",
        "@import",
        "&amp;",
        "{{ x }}",
        "\u202eRTL",
    ],
)
def test_injection_corpus_stays_in_text_nodes(injection):
    card = validate_card(
        {
            "title": "safe",
            "subtitle": "safe",
            "blocks": [
                {"type": "stats", "tiles": [{"label": "safe", "value": "safe", "note": "safe"}]},
                {"type": "heading", "text": "safe"},
                {"type": "paragraph", "text": "safe"},
                {"type": "bullets", "items": ["safe"]},
                {
                    "type": "table",
                    "columns": [{"header": "safe", "align": "end"}],
                    "rows": [["safe"]],
                    "total": ["safe"],
                },
                {"type": "callout", "tone": "info", "text": "safe"},
                {
                    "type": "sources",
                    "items": [{"title": "safe", "url": "https://example.com/safe"}],
                },
            ],
        }
    )
    fixed = Tags()
    fixed.feed(render_card_html(card))
    card.title = card.subtitle = injection
    card.blocks[0].tiles[0].label = injection
    card.blocks[0].tiles[0].value = injection
    card.blocks[0].tiles[0].note = injection
    card.blocks[1].text = injection
    card.blocks[2].text = injection
    card.blocks[3].items[0] = injection
    card.blocks[4].columns[0].header = injection
    card.blocks[4].rows[0][0] = injection
    card.blocks[4].total[0] = injection
    card.blocks[5].text = injection
    card.blocks[6].items[0].title = injection
    card.blocks[6].items[0].url = "https://example.com/" + injection

    parser = Tags()
    output = render_card_html(card)
    parser.feed(output)
    allowed_tags = {
        "html",
        "head",
        "meta",
        "style",
        "body",
        "main",
        "header",
        "div",
        "section",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "ul",
        "li",
        "span",
        "br",
    }
    allowed_attributes = {
        "charset",
        "http-equiv",
        "content",
        "name",
        "class",
        "data-block",
        "data-row",
    }
    fixed_values = {value for _tag, attrs in fixed.tags for value in attrs.values()}
    assert {tag for tag, _attrs in parser.tags} <= allowed_tags
    for _tag, attrs in parser.tags:
        assert set(attrs) <= allowed_attributes
        for value in attrs.values():
            assert value in fixed_values
            assert value is None or injection not in value
    css = output.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "url(" not in css and "@import" not in css


def test_mutated_alignment_is_rejected_and_layout_tokens_are_fixed():
    card = validate_card(
        {
            "title": "Header",
            "subtitle": "Subtitle",
            "blocks": [
                {
                    "type": "stats",
                    "tiles": [{"label": "A", "value": "1"}, {"label": "B", "value": "2"}],
                },
                {
                    "type": "table",
                    "columns": [{"header": "Name", "align": "start"}],
                    "rows": [["x"]],
                },
                {"type": "sources", "items": [{"title": "Site", "url": "https://example.com"}]},
            ],
        }
    )
    output = render_card_html(card)
    assert "grid-template-columns:repeat(auto-fit,minmax(150px,1fr))" in output
    assert "header{border-bottom:3px solid" in output
    assert "margin-top:4px" in output
    assert '<div class="meta">Sources</div>' in output
    card.blocks[1].columns[0].align = 'end" onclick="x'
    with pytest.raises(CardError, match="align: unsupported"):
        render_card_html(card)
