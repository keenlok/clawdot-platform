import pytest
from chatcore_google.itinerary import HEADER, VERSION, normalize_groups, summarize, tracker_rows
from chatcore_google.settings import GoogleSettings


def row(day="2026-12-01", time="09:00-10:00", place="Museum", cost="SGD 12.50", status="Confirmed"):
    return [day, time, place, "Sight", cost, "", status, ""]


def test_tracker_version_and_header_are_required_per_tab():
    assert tracker_rows([VERSION, HEADER, row()]) == [row()]
    for data in [[HEADER, row()], [["chatcore-itinerary", "2"], HEADER], [VERSION, ["Other"]]]:
        with pytest.raises(ValueError, match="header|version"):
            tracker_rows(data)


def test_blank_rows_preserve_sheet_row_numbers_and_trailing_blank_cells_are_ignored():
    values = [VERSION, HEADER, [], [*row(), "", ""], [" "]]
    result = summarize([("Tokyo", tracker_rows(values))])
    assert result["days"]["2026-12-01"][0]["row"] == 4
    assert result["warnings"] == []
    assert result["planned_totals"] == {"SGD": "12.50"}


def test_trip_summary_aggregates_tabs_crosses_midnight_and_preserves_currencies():
    tabs = [
        ("Tokyo", [row(time="23:30-01:00"), row(day="2026-12-03", cost="JPY 1200")]),
        (
            "Kyoto",
            [
                row(day="2026-12-02", time="00:30-02:00", cost="USD 5.25"),
                row(cost="SGD 999", status="Dropped"),
            ],
        ),
    ]
    result = summarize(tabs)
    assert len(result["clashes"]) == 1
    assert result["planned_totals"] == {"SGD": "12.50", "JPY": "1200", "USD": "5.25"}
    assert any("afternoon" in gap for gap in result["gaps"])
    assert len(result["days"]) == 3


def test_missing_and_malformed_fields_are_reported_without_guesses():
    result = summarize(
        [
            (
                "Day 1",
                [
                    row(day="Day 1", time="", cost="unknown"),
                    row(day="Day 3", time="bad", cost="12"),
                ],
            )
        ],
        partial=True,
    )
    assert result["warnings"] and any("Day 2" in gap for gap in result["gaps"])
    assert result["planned_totals"] == {} and result["partial"]


def test_unknown_costs_mark_totals_incomplete_and_stays_do_not_clash_with_sights():
    hotel = ["2026-12-01", "00:00-23:59", "Hotel", "Stay", "unknown", "", "Confirmed", ""]
    result = summarize([("Tokyo", [hotel, row()])])
    assert result["totals_partial"]
    assert result["clashes"] == []


def test_user_directed_tab_groups_are_validated_and_proposals_force_status():
    groups = [
        {
            "title": "Tokyo",
            "rows": [
                {
                    "day": "Day 1",
                    "place": "Museum",
                    "type": "Sight",
                    "status": "Confirmed",
                    "source_url": "https://example.com/museum",
                }
            ],
        }
    ]
    result = normalize_groups(
        groups, GoogleSettings(), suggest=True, sources={"https://example.com/museum"}
    )
    assert result[0]["title"] == "Tokyo"
    assert result[0]["rows"][0][6] == "Proposed"
    assert "unverified" in result[0]["rows"][0][7].lower()
    assert "https://example.com/museum" in result[0]["rows"][0][7]
    with pytest.raises(ValueError, match="source"):
        normalize_groups(groups, GoogleSettings(), suggest=True, sources=set())


@pytest.mark.parametrize(
    "groups",
    [
        [],
        [{"title": "Bad/Name", "rows": []}],
        [{"title": "Day 1", "rows": [{"status": "Not real"}]}],
        [{"title": "Day 1", "rows": [{}]}, {"title": "Day 1", "rows": [{}]}],
    ],
)
def test_empty_bad_or_duplicate_tabs_and_invalid_rows_are_refused(groups):
    with pytest.raises(ValueError):
        normalize_groups(groups, GoogleSettings())
