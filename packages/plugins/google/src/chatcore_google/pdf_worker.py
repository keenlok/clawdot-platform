"""Short-lived, bounded PDF parser. This module has no bot or credential wiring."""

from __future__ import annotations

import io
import json
import sys


def _set_limits():
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))


def extract_pdf(data: bytes, max_chars: int) -> dict:
    """Extract selectable text only; never invoke embedded files or links."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return {"error": "encrypted"}
        total = len(reader.pages)
        read = empty = 0
        chunks = []
        remaining = max_chars - 256
        partial = total > 50
        for page in reader.pages[:50]:
            text = page.extract_text() or ""
            read += 1
            if not text.strip():
                empty += 1
            chunk = f"[Page {read}]\n{text}"
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                partial = True
                break
            chunks.append(chunk)
            remaining -= len(chunk) + 1
            if remaining <= 0:
                partial = read < total
                break
        partial = partial or bool(empty) or not read
        header = (
            f"PDF pages read: {read}/{total}; pages without text: {empty}; "
            f"partial: {str(partial).lower()}.\n"
        )
        if empty or not read:
            header += "Some pages have no extractable text; scanned images need OCR.\n"
        header += "Selectable text only; visual layout and images are not interpreted.\n"
        return {"text": (header + "\n".join(chunks))[:max_chars]}
    except Exception:
        # Remote bytes and parser diagnostics never cross the process boundary.
        return {"error": "malformed"}


def main():
    _set_limits()
    max_chars = int(sys.argv[1])
    if not 256 <= max_chars <= 100_000:
        raise SystemExit(2)
    data = sys.stdin.buffer.read(10_000_001)
    if len(data) > 10_000_000:
        raise SystemExit(2)
    result = extract_pdf(data, max_chars)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
