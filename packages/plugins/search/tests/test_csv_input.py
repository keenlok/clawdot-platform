import pytest
from chatcore_search.csv_input import parse_csv


def test_csv_preserves_bom_quotes_newlines_and_empty_cells():
    result = parse_csv(b'\xef\xbb\xbfDay,Place,Notes\r\n1,"A, B","line1\nline2"\r\n2,,\r\n')
    assert result.rows == [["Day", "Place", "Notes"], ["1", "A, B", "line1\nline2"], ["2", "", ""]]
    assert not result.truncated


def test_csv_limits_return_complete_rows_and_mark_partial_coverage():
    result = parse_csv(b"a,b,c\n1,2,3\n4,5,6\n", max_rows=2, max_columns=2)
    assert result.rows == [["a", "b"], ["1", "2"]]
    assert result.truncated


@pytest.mark.parametrize("data", [b'"never closed', b"a,\xff", b"a,\x00"])
def test_csv_rejects_malformed_or_unsupported_input(data):
    with pytest.raises(ValueError):
        parse_csv(data)


def test_csv_refuses_oversized_input_and_fields():
    with pytest.raises(ValueError, match="size"):
        parse_csv(b"123456", max_bytes=5)
    with pytest.raises(ValueError, match="field"):
        parse_csv(b"123456", max_field_chars=5)


def test_csv_does_not_evaluate_formula_cells():
    assert parse_csv(b'Name,Value\nX,=IMPORTDATA("https://example.com")').rows[1][1].startswith("=")


async def test_csv_reader_reports_encoding_errors_plainly():
    from chatcore.embed import FakeEmbedder
    from chatcore.events import Attachment, AttachmentKind
    from chatcore.testing import GROUP, FakePlatform, memory_stores, message
    from chatcore.tools.registry import ToolContext
    from chatcore_search import SearchPlugin

    stores, platform = memory_stores(), FakePlatform()
    attachment = Attachment("bad", AttachmentKind.DOCUMENT, filename="bad.csv")
    incoming = message("Read this CSV", attachments=[attachment])
    await stores.repo.log_message(incoming)
    platform.files["bad"] = b"Column\n\xff\n"
    result = await SearchPlugin(FakeEmbedder()).read_attachment(
        ToolContext(GROUP, incoming.user, False, platform, stores), message_id=incoming.message_id
    )
    assert result.is_error and "UTF-8" in result.content
