from types import SimpleNamespace

import pytest
from chatcore_google.refs import approved_ref, approved_source_ref, parse_google_ref
from chatcore_google.render import doc_text, literal_rows


@pytest.mark.parametrize(
    "url,file_id,gid",
    [
        ("https://docs.google.com/spreadsheets/u/0/d/abc-/edit#gid=7", "abc-", 7),
        (
            "https://docs.google.com/spreadsheets/d/abc-/edit?gid=464258837#gid=464258837",
            "abc-",
            464258837,
        ),
        ("https://docs.google.com/document/d/xyz/edit?tab=t.abc", "xyz", None),
        ("https://drive.google.com/file/d/a_b/view", "a_b", None),
        ("https://drive.google.com/open?id=foo", "foo", None),
    ],
)
def test_google_link_forms(url, file_id, gid):
    ref = parse_google_ref(url)
    assert ref.file_id == file_id and ref.gid == gid


@pytest.mark.parametrize(
    "url",
    [
        "abc",
        "http://docs.google.com/document/d/a",
        "https://docs.google.com.evil.test/document/d/a",
        "https://x@docs.google.com/document/d/a",
        "https://docs.google.com:444/document/d/a",
        "https://docs.google.com/document/d/a%2fb",
    ],
)
def test_google_link_rejects_forged_targets(url):
    with pytest.raises(ValueError):
        parse_google_ref(url)


def test_provenance_matches_file_identity_but_never_search_urls():
    human = frozenset({"https://drive.google.com/open?id=abc"})
    assert approved_ref("https://docs.google.com/spreadsheets/d/abc/edit#gid=9", human).gid == 9
    with pytest.raises(ValueError, match="human"):
        approved_ref("https://docs.google.com/spreadsheets/d/other", human)


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.google.com/spreadsheets/d/sheet/edit?gid=1#gid=2",
        "https://docs.google.com/spreadsheets/d/sheet/edit#gid=1&gid=2",
        "https://docs.google.com/spreadsheets/d/sheet/edit#gid=abc",
    ],
)
def test_google_link_rejects_conflicting_or_non_digit_gids(url):
    with pytest.raises(ValueError):
        parse_google_ref(url)


def test_google_link_accepts_duplicate_gid_in_one_source():
    assert (
        parse_google_ref("https://docs.google.com/spreadsheets/d/sheet/edit#gid=1&gid=1").gid == 1
    )


@pytest.mark.parametrize(
    "requested",
    [
        "https://docs.google.com/spreadsheets/d/sheet/edit?gid=7#gid=7",
        "https://docs.google.com/spreadsheets/d/sheet/edit",
    ],
)
async def test_approved_source_accepts_duplicate_gid_link(requested):
    chat = SimpleNamespace(key="tg:chat")
    message = SimpleNamespace(
        message_id="m1",
        chat=chat,
        thread_id=None,
        is_bot=False,
        text="Itinerary https://docs.google.com/spreadsheets/d/sheet/edit?gid=7#gid=7",
    )

    class Repo:
        async def get_message(self, _chat, _message_id):
            return message

    ctx = SimpleNamespace(stores=SimpleNamespace(repo=Repo()), chat=chat, thread_id=None)
    assert (await approved_source_ref(requested, ctx, "m1")).file_id == "sheet"


def test_docs_nested_tabs_and_tables_are_flattened_with_labels():
    lunch = {"paragraph": {"elements": [{"textRun": {"content": "Lunch"}}]}}
    table = {"table": {"tableRows": [{"tableCells": [{"content": [lunch]}]}]}}
    document = {
        "title": "Trip",
        "tabs": [
            {
                "tabProperties": {"title": "Day 1"},
                "documentTab": {
                    "body": {
                        "content": [
                            {"paragraph": {"elements": [{"textRun": {"content": "Hello\n"}}]}}
                        ]
                    }
                },
                "childTabs": [
                    {
                        "tabProperties": {"title": "Food"},
                        "documentTab": {"body": {"content": [table]}},
                    }
                ],
            }
        ],
    }
    text = doc_text(document, max_chars=1000)
    assert "Day 1" in text and "Food" in text and "Lunch" in text and "Hello" in text
    assert "[Truncated]" in doc_text(document, max_chars=8)


def test_sheet_values_are_literal_string_values():
    values = literal_rows(
        [['=IMPORTXML("x")', "-5 apples", "+65 1234", "@here", "normal"], [12, True]]
    )
    assert [cell["userEnteredValue"]["stringValue"] for cell in values[0]["values"]] == [
        '=IMPORTXML("x")',
        "-5 apples",
        "+65 1234",
        "@here",
        "normal",
    ]
    assert values[1]["values"][0] == {"userEnteredValue": {"numberValue": 12}}
    with pytest.raises(ValueError):
        literal_rows([[{"formulaValue": "=BAD()"}]])
    with pytest.raises(ValueError, match="number|range"):
        literal_rows([[10**400]])
