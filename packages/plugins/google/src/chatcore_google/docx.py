"""Bounded, main-body-only DOCX text extraction; never resolves package links."""

from __future__ import annotations

import io
import zipfile
import zlib
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse

_WORD = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_XML_BYTES = 8 * 1024 * 1024
_COVERAGE = (
    "[DOCX main body only: images/drawings, headers/footers, comments, footnotes, "
    "directly hidden/deleted text and visual layout omitted; styles not interpreted; "
    "tab/heading fragments do not select a section.]\n"
)


def _xml(data):
    iterator = iterparse(
        io.BytesIO(data),
        events=("start", "end"),
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )
    depth = count = 0
    for event, _ in iterator:
        if event == "start":
            depth += 1
            count += 1
            if depth > 64 or count > 100_000:
                raise ValueError("DOCX XML complexity exceeds the limit.")
        else:
            depth -= 1
    return iterator.root


def _part(package, name):
    with package.open(name) as member:
        data = member.read(_XML_BYTES + 1)
    if len(data) > _XML_BYTES:
        raise ValueError("DOCX XML exceeds the size limit.")
    return _xml(data)


def _children(element):
    for child in element:
        if child.tag == _MC + "AlternateContent":
            branch = child.find(_MC + "Fallback")
            if branch is None:
                branch = child.find(_MC + "Choice")
            if branch is not None:
                yield from _children(branch)
        else:
            yield child


def _inline(element, w):
    if element.tag in {w + tag for tag in ("del", "moveFrom", "drawing", "pict", "object")}:
        return ""
    if element.tag == w + "r":
        properties = element.find(w + "rPr")
        if properties is not None:
            for name in ("vanish", "webHidden"):
                flag = properties.find(w + name)
                if flag is not None and flag.get(w + "val", "true").lower() not in {
                    "0",
                    "false",
                    "off",
                }:
                    return ""
    if element.tag == w + "t":
        return element.text or ""
    if element.tag == w + "tab":
        return "\t"
    if element.tag in {w + "br", w + "cr"}:
        return "\n"
    return "".join(_inline(child, w) for child in _children(element))


def _blocks(element, w):
    for child in _children(element):
        if child.tag == w + "p":
            yield _inline(child, w)
        elif child.tag == w + "tbl":
            yield "[Table]"
            for row in _children(child):
                if row.tag != w + "tr":
                    continue
                cells = []
                for cell in _children(row):
                    if cell.tag != w + "tc":
                        continue
                    text = " / ".join(_blocks(cell, w))
                    properties = cell.find(w + "tcPr")
                    if properties is not None:
                        span = properties.find(w + "gridSpan")
                        merge = properties.find(w + "vMerge")
                        if span is not None or merge is not None:
                            text += " [merged cell]"
                    cells.append(text)
                yield " | ".join(cells)
        elif child.tag not in {w + tag for tag in ("del", "moveFrom", "sectPr")}:
            yield from _blocks(child, w)


def docx_text(data: bytes, *, max_chars: int) -> str:
    """Return capped, coverage-labelled visible main-body paragraph/table text."""
    marker = "\n[Truncated: partial DOCX text]"
    if max_chars < len(_COVERAGE) + len(marker):
        raise ValueError("DOCX character limit is too small for coverage information.")
    try:
        if len(data) > 10_000_000:
            raise ValueError("DOCX exceeds the size limit.")
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            entries = package.infolist()
            names = [entry.filename for entry in entries]
            if (
                len(entries) > 1024
                or len(names) != len(set(names))
                or sum(entry.file_size for entry in entries) > 16 * 1024 * 1024
            ):
                raise ValueError("DOCX archive exceeds limits or has duplicate parts.")
            for entry in entries:
                if (
                    entry.flag_bits & 1
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or entry.file_size > 200 * max(entry.compress_size, 1)
                    or entry.filename.startswith("/")
                    or ".." in entry.filename.replace("\\", "/").split("/")
                    or entry.filename.casefold().rsplit("/", 1)[-1]
                    in {"vbaproject.bin", "vbadata.xml"}
                ):
                    raise ValueError("DOCX archive contains unsupported or unsafe parts.")
            for name in ("[Content_Types].xml", "word/document.xml"):
                if package.getinfo(name).file_size > _XML_BYTES:
                    raise ValueError("DOCX XML exceeds the size limit.")
            types = _part(package, "[Content_Types].xml")
            if types.tag != _CT + "Types" or not any(
                item.tag == _CT + "Override"
                and item.get("PartName") == "/word/document.xml"
                and item.get("ContentType") == _MAIN
                for item in types
            ):
                raise ValueError("DOCX main document type is unsupported.")
            root = _part(package, "word/document.xml")
        namespace = root.tag.removesuffix("}document").removeprefix("{")
        if namespace not in _WORD or root.tag != "{" + namespace + "}document":
            raise ValueError("DOCX main document is unsupported.")
        w = "{" + namespace + "}"
        body = root.find(w + "body")
        if body is None:
            raise ValueError("DOCX main body is missing.")
        text = _COVERAGE
        for block in _blocks(body, w):
            if len(text) + len(block) + 1 > max_chars:
                return (text + block)[: max_chars - len(marker)] + marker
            text += block + "\n"
        return text.rstrip()
    except (
        zipfile.BadZipFile,
        LookupError,
        RuntimeError,
        NotImplementedError,
        ParseError,
        DefusedXmlException,
        OSError,
        EOFError,
        UnicodeError,
        zlib.error,
    ):
        raise ValueError(
            "DOCX could not be read: invalid, unsafe or unsupported document."
        ) from None
