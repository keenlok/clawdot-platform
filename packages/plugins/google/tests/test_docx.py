import io
import struct
import zipfile

import pytest

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"
TYPES = (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)


def archive(body="", *, namespace=W, xml=None, types=TYPES, extra=(), compression=0):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as package:
        if types is not None:
            package.writestr("[Content_Types].xml", types)
        if xml is not None or body is not None:
            package.writestr(
                "word/document.xml",
                xml
                if xml is not None
                else f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>',
            )
        for name, value in extra:
            package.writestr(name, value)
    return output.getvalue()


def read(data, max_chars=30000):
    from chatcore_google.docx import docx_text

    return docx_text(data, max_chars=max_chars)


@pytest.mark.parametrize("namespace", [W, STRICT])
def test_paragraphs_tables_empty_cells_and_order(namespace):
    result = read(
        archive(
            "<w:p><w:r><w:t>Before</w:t><w:tab/><w:t>tab</w:t><w:br/><w:t>line</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Tokyo</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Morning</w:t></w:r></w:p></w:tc><w:tc><w:p/></w:tc>"
            "<w:tc><w:p><w:hyperlink><w:r><w:t>Display link</w:t></w:r></w:hyperlink></w:p></w:tc>"
            "</w:tr></w:tbl><w:p><w:r><w:t>After</w:t></w:r></w:p>",
            namespace=namespace,
        )
    )
    assert result.index("Before\ttab\nline") < result.index("Tokyo") < result.index("After")
    assert "Tokyo / Morning |  | Display link" in result
    assert "main body" in result and "images" in result and "headers" in result


def test_hidden_deleted_drawings_and_alternate_content_are_not_duplicated():
    result = read(
        archive(
            "<w:p><w:r><w:t>Visible</w:t></w:r>"
            "<w:r><w:rPr><w:vanish/></w:rPr><w:t>HIDDEN</w:t></w:r>"
            '<w:r><w:rPr><w:vanish w:val="false"/></w:rPr><w:t>Shown</w:t></w:r>'
            "<w:del><w:r><w:t>DELETED</w:t></w:r></w:del>"
            "<w:r><w:drawing><w:t>DRAWING</w:t></w:drawing></w:r>"
            '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            '<mc:Choice Requires="w"><w:r><w:t>CHOICE</w:t></w:r></mc:Choice>'
            "<mc:Fallback><w:r><w:t>Fallback</w:t></w:r></mc:Fallback>"
            "</mc:AlternateContent></w:p>"
        )
    )
    assert "VisibleShownFallback" in result
    assert all(word not in result for word in ["HIDDEN", "DELETED", "DRAWING", "CHOICE"])


def test_merged_and_nested_tables_keep_text_once():
    result = read(
        archive(
            '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
            "<w:p><w:r><w:t>Merged</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Nested</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:tc></w:tr><w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>"
            "</w:tr></w:tbl>"
        )
    )
    assert result.count("Merged") == result.count("Nested") == 1
    assert "merged" in result.lower()


def test_truncation_is_explicit_and_included_in_character_cap():
    result = read(archive("<w:p><w:r><w:t>" + "abc " * 1000 + "</w:t></w:r></w:p>"), 300)
    assert len(result) <= 300 and "Truncated" in result and "main body" in result


def test_unused_image_name_containing_vba_is_not_mistaken_for_a_macro():
    result = read(
        archive("<w:p><w:r><w:t>Body</w:t></w:r></w:p>", extra=[("word/media/vbar.png", b"image")])
    )
    assert "Body" in result


@pytest.mark.parametrize(
    "data",
    [
        b"not a zip",
        archive(body=None),
        archive(types=None),
        archive(xml="<broken>"),
        archive(xml="<other/>"),
        archive(types=TYPES.replace("document.main+xml", "template.main+xml")),
        archive(extra=[("word/vbaProject.bin", "macro")]),
        archive(extra=[("../evil", "traversal")]),
    ],
)
def test_invalid_or_unsupported_packages_fail_safely(data):
    with pytest.raises(ValueError, match="DOCX"):
        read(data)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_dtd_and_entities_are_rejected_for_both_encodings(encoding):
    xml = (
        '<?xml version="1.0" encoding="' + encoding + '"?>'
        '<!DOCTYPE document [<!ENTITY value "secret-data">]>'
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&value;</w:t>'
        "</w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(ValueError, match="DOCX") as error:
        read(archive(xml=xml.encode(encoding)))
    assert "secret-data" not in str(error.value)


def test_zip_bomb_and_too_many_entries_are_rejected():
    with pytest.raises(ValueError, match="DOCX"):
        read(archive("<w:p>" + "a" * 100000 + "</w:p>", compression=zipfile.ZIP_DEFLATED))
    with pytest.raises(ValueError, match="DOCX"):
        read(archive(extra=[(f"unused/{i}", "") for i in range(1024)]))


def test_xml_size_total_size_depth_and_nodes_are_bounded():
    inputs = [
        archive(xml=b"a" * (8 * 1024 * 1024 + 1)),
        archive(extra=[("unused", b"a" * (16 * 1024 * 1024))]),
        archive("<w:sdt>" * 70 + "</w:sdt>" * 70),
        archive("<w:p/>" * 100001),
    ]
    for data in inputs:
        with pytest.raises(ValueError, match="DOCX"):
            read(data)


def test_duplicate_main_part_encryption_and_unsupported_compression_are_rejected():
    with pytest.warns(UserWarning, match="Duplicate"):
        duplicate = archive(extra=[("word/document.xml", "duplicate")])
    original = archive()
    central = original.index(b"PK\x01\x02")
    encrypted = bytearray(original)
    struct.pack_into("<H", encrypted, central + 8, 1)
    unsupported = bytearray(original)
    struct.pack_into("<H", unsupported, central + 10, 99)
    for data in [duplicate, bytes(encrypted), bytes(unsupported)]:
        with pytest.raises(ValueError, match="DOCX"):
            read(data)


def test_unknown_xml_encoding_and_broken_deflate_are_safe_errors():
    data = archive(xml=b'<?xml version="1.0" encoding="unknown-secret-encoding"?><doc/>')
    with pytest.raises(ValueError, match="DOCX") as error:
        read(data)
    assert "secret" not in str(error.value)
    data = bytearray(archive(compression=zipfile.ZIP_DEFLATED))
    offset = 30 + len("[Content_Types].xml")
    data[offset : offset + 4] = b"\xff\xff\xff\xff"
    with pytest.raises(ValueError, match="DOCX"):
        read(bytes(data))
