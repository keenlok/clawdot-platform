import re

from chatcore.llm.types import Usage
from chatcore_research.agent import ResearchReport, Source


def test_report_renumbers_citations_and_removes_model_urls():
    report = ResearchReport(
        topic="topic",
        text="Finding [S2] then [S5] and unknown [S9] https://evil.example/?q=secret",
        sources=(
            Source(2, "https://two.example", "Second"),
            Source(5, "https://five.example", "Fifth"),
        ),
        usage=Usage(),
        complete=False,
    )

    rendered = report.render(max_chars=1000)

    assert "Finding [1] then [2] and unknown  [link removed]" in rendered
    assert "1. Second — https://two.example" in rendered
    assert "2. Fifth — https://five.example" in rendered
    assert "Gaps and uncertainty:\n- Not stated by the research model" in rendered
    assert "stopped at its budget" in rendered
    assert "https://evil.example" not in rendered


def test_report_limits_rendered_length_and_keeps_source_links():
    report = ResearchReport(
        topic="topic",
        text="A" * 1000 + "\n\nGaps and uncertainty:\n- unclear",
        sources=(Source(1, "https://source.example", "Title"),),
        usage=Usage(),
        complete=True,
        fetched_sources=(Source(1, "https://source.example", "Title"),),
    )

    rendered = report.render(max_chars=180)

    assert len(rendered) <= 180
    assert "https://source.example" in rendered
    assert "…" in rendered


def test_report_sanitizes_urls_from_topic():
    report = ResearchReport(
        topic="https://evil.example/?secret=1 prices",
        text="Findings.",
        sources=(),
        usage=Usage(),
        complete=True,
    )

    rendered = report.render()

    assert "[link removed] prices" in rendered
    assert "https://evil.example" not in rendered


def test_long_cited_source_urls_are_kept_whole_without_dropping_or_renumbering():
    long_url = "https://first.example/" + "x" * 400
    report = ResearchReport(
        topic="topic",
        text="First [S1] and second [S2].",
        sources=(
            Source(1, long_url, "First source title"),
            Source(2, "https://second.example/path", "Second source title"),
        ),
        usage=Usage(),
        complete=True,
    )

    rendered = report.render(max_chars=800)
    lines = [line for line in rendered.splitlines() if re.match(r"\d+\. ", line)]

    assert "First [1] and second [2]." in rendered
    assert len(lines) == 2
    assert lines[0].endswith(long_url)
    assert lines[0].startswith("1. ")
    assert lines[1] == "2. Second source title — https://second.example/path"
