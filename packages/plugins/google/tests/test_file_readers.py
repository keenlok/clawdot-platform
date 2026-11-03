import asyncio
import io
import json
import sys

import pytest
from chatcore_google import file_readers
from chatcore_google.settings import GoogleSettings
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def pdf_bytes(texts, *, encrypted=False):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("not-shared")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_csv_preserves_bom_quotes_empty_cells_and_formula_text():
    text = file_readers.read_csv(
        '\ufeffday,activity,cost\r\n1,"food, tea",\r\n2,"line 1\nline 2",=1+1\r\n'.encode(),
        GoogleSettings(),
    )
    rows = json.loads(text.split("\n", 1)[1])
    assert rows == [
        ["day", "activity", "cost"],
        ["1", "food, tea", ""],
        ["2", "line 1\nline 2", "=1+1"],
    ]
    assert "rows read: 3" in text
    assert "partial: false" in text


@pytest.mark.parametrize("data", [b'one,"unterminated', b"name\n\xff"])
def test_csv_rejects_invalid_encoding_and_quotes(data):
    with pytest.raises(ValueError, match="CSV"):
        file_readers.read_csv(data, GoogleSettings())


@pytest.mark.parametrize(
    ("data", "settings", "error"),
    [
        (b"a,b,c,d,e,f,g,h,i", GoogleSettings(max_columns=8), "column"),
        (b"x" * 101, GoogleSettings(max_cell_chars=100), "cell"),
        (b"", GoogleSettings(), "empty"),
    ],
)
def test_csv_rejects_invalid_shape(data, settings, error):
    with pytest.raises(ValueError, match=error):
        file_readers.read_csv(data, settings)


def test_csv_reports_bounded_rows_and_output():
    text = file_readers.read_csv(b"first\nsecond\nthird", GoogleSettings(max_rows=2))
    assert "rows read: 2" in text and "partial: true" in text
    assert "third" not in text
    text = file_readers.read_csv(
        ("x" * 900 + "\n" + "y" * 900).encode(), GoogleSettings(max_chars=1000)
    )
    assert len(text) <= 1000 and "partial: true" in text
    assert len(json.loads(text.split("\n", 1)[1])) == 1


async def test_pdf_extracts_text_and_page_coverage():
    text = await file_readers.read_pdf(
        pdf_bytes(["Hong Kong itinerary", "Day two"]), max_chars=1000
    )
    assert "Hong Kong itinerary" in text and "Day two" in text
    assert "pages read: 2/2" in text and "partial: false" in text


async def test_pdf_marks_scanned_or_empty_and_mixed_text_pages():
    text = await file_readers.read_pdf(pdf_bytes([""]), max_chars=1000)
    assert "no extractable text" in text and "OCR" in text
    text = await file_readers.read_pdf(pdf_bytes(["HK", ""]), max_chars=1000)
    assert "HK" in text and "pages without text: 1" in text and "partial: true" in text


async def test_pdf_without_pages_reports_no_coverage():
    text = await file_readers.read_pdf(pdf_bytes([]), max_chars=1000)
    assert "pages read: 0/0" in text and "partial: true" in text


async def test_pdf_caps_pages_and_characters():
    text = await file_readers.read_pdf(pdf_bytes(["Day"] * 51), max_chars=5000)
    assert "pages read: 50/51" in text and "partial: true" in text
    text = await file_readers.read_pdf(pdf_bytes(["x" * 5000]), max_chars=1000)
    assert len(text) <= 1000 and "partial: true" in text


@pytest.mark.parametrize("data", [b"not a pdf", pdf_bytes(["HK"], encrypted=True)])
async def test_pdf_rejects_invalid_or_encrypted_files(data):
    with pytest.raises(ValueError, match="PDF"):
        await file_readers.read_pdf(data, max_chars=1000)


async def test_pdf_timeout_kills_and_reaps_with_minimal_environment(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []
    captured = {}

    async def launch(*args, **kwargs):
        captured.update(kwargs)
        child = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        children.append(child)
        return child

    monkeypatch.setenv("PG_DSN", "never-inherit-this")
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS_FILE", "/private/profile")
    monkeypatch.setattr(file_readers.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(file_readers, "PDF_TIMEOUT_S", 0.05)
    with pytest.raises(ValueError, match="time"):
        await file_readers.read_pdf(b"input", max_chars=1000)
    assert children[0].returncode is not None
    assert "PG_DSN" not in captured["env"]
    assert "GOOGLE_OAUTH_CREDENTIALS_FILE" not in captured["env"]


async def test_pdf_output_overflow_kills_and_reaps(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []

    async def launch(*args, **kwargs):
        child = await original(
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(30)",
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(file_readers.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(ValueError, match="output"):
        await file_readers.read_pdf(b"input", max_chars=1000)
    assert children[0].returncode is not None


async def test_pdf_cancel_reaps_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    children = []

    async def launch(*args, **kwargs):
        child = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(file_readers.asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(file_readers.read_pdf(b"input", max_chars=1000))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].returncode is not None
