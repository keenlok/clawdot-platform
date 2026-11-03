import pytest
from chatcore_google.refs import approved_ref, parse_google_ref
from chatcore_google.render import doc_text, literal_rows


@pytest.mark.parametrize(
    "url,file_id,gid",
    [
        ("https://docs.google.com/spreadsheets/u/0/d/abc-/edit#gid=7", "abc-", 7),
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


def test_sheet_values_are_literal_and_formula_neutralized():
    values = literal_rows([['=IMPORTDATA("x")', "+a", "-b", "@c", "normal"], [12, True]])
    assert values[0]["values"][0] == {"userEnteredValue": {"stringValue": '\'=IMPORTDATA("x")'}}
    assert values[1]["values"][0] == {"userEnteredValue": {"numberValue": 12}}
    with pytest.raises(ValueError):
        literal_rows([[{"formulaValue": "=BAD()"}]])
    with pytest.raises(ValueError, match="number|range"):
        literal_rows([[10**400]])
