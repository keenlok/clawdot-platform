"""Block private Telegram identifiers in staged additions and commit messages."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SERVICE_PATHS = {
    "addemoji",
    "addlist",
    "addstickers",
    "addtheme",
    "botfather",
    "joinchat",
    "proxy",
    "setlanguage",
    "share",
    "socks",
}
SKIP_MARKER = "private-identifier: ok"
CHAT_ID = re.compile(r"(?<![\w])tg:-100(\d{10,})(?!\d)", re.IGNORECASE)
USER_ID = re.compile(r"(?<![\w])tg:(\d{7,})(?!\d)", re.IGNORECASE)
TELEGRAM_LINK = re.compile(r"(?i)(?<!\w)t\.me/([A-Za-z0-9_]{5,})")


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    kind: str


def _is_placeholder(digits: str) -> bool:
    ascending = "1234567890"[: len(digits)]
    return digits == ascending or len(set(digits)) == 1


def _patterns(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for match in CHAT_ID.finditer(text):
        if not _is_placeholder(match.group(1)):
            found.append((match.start(), "telegram chat id"))
    for match in USER_ID.finditer(text):
        if not _is_placeholder(match.group(1)):
            found.append((match.start(), "telegram user id"))
    for match in TELEGRAM_LINK.finditer(text):
        if match.group(1).casefold() not in SERVICE_PATHS:
            found.append((match.start(), "telegram link"))
    return sorted(found)


def _term_pattern(term: str) -> re.Pattern[str]:
    normalized = term.strip()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    return re.compile(rf"(?<!\w)@?{re.escape(normalized)}(?!\w)", re.IGNORECASE)


def scan_text(text: str, denylist_terms: Iterable[str] = ()) -> list[Hit]:
    hits: list[Hit] = []
    term_patterns = [_term_pattern(term) for term in denylist_terms if term.strip().lstrip("@")]
    for line_number, line in enumerate(text.splitlines(), 1):
        if SKIP_MARKER in line:
            continue
        for _, kind in _patterns(line):
            hits.append(Hit("", line_number, kind))
        for pattern in term_patterns:
            if pattern.search(line):
                hits.append(Hit("", line_number, "denylisted term"))
    return hits


def load_denylist(path: str | Path) -> list[str]:
    try:
        contents = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        print(
            f"warning: private identifier denylist could not be read ({type(exc).__name__})",
            file=sys.stderr,
        )
        return []
    return [
        line.strip().removeprefix("@").strip()
        for line in contents.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and line.strip().lstrip("@")
    ]


def _path_from_diff_header(line: str) -> str:
    value = line[len("diff --git ") :]
    if value.startswith('"'):
        # Git quotes C-style path names. shlex handles ordinary quoted paths;
        # uncommon escaped path bytes still remain a harmless diagnostic label.
        import shlex

        parts = shlex.split(value)
        candidate = parts[-1] if len(parts) > 1 else value
    else:
        candidate = value.split(" b/", 1)[-1]
    if candidate.startswith("b/"):
        candidate = candidate[2:]
    return candidate


def scan_added_diff(diff: str, denylist_terms: Iterable[str] = ()) -> list[Hit]:
    hits: list[Hit] = []
    path = ""
    new_line = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = _path_from_diff_header(line)
            in_hunk = False
        elif line.startswith("@@ "):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            new_line = int(match.group(1)) if match else 0
            in_hunk = True
        elif not in_hunk:
            continue
        elif line.startswith("+++") or line.startswith("\\"):
            continue
        elif line.startswith("+"):
            for hit in scan_text(line[1:], denylist_terms):
                hits.append(Hit(path, new_line, hit.kind))
            new_line += 1
        elif line.startswith("-"):
            continue
        else:
            new_line += 1
    return hits


def scan_commit_message(message: str, denylist_terms: Iterable[str] = ()) -> list[Hit]:
    original_lines = message.splitlines()
    hits: list[Hit] = []
    term_patterns = [_term_pattern(term) for term in denylist_terms if term.strip().lstrip("@")]
    for line_number, line in enumerate(original_lines, 1):
        if line.lstrip().startswith("#") or re.match(r"(?i)^\s*Co-Authored-By\s*:", line):
            continue
        if SKIP_MARKER in line:
            continue
        for _, kind in _patterns(line):
            hits.append(Hit("", line_number, kind))
        for pattern in term_patterns:
            if pattern.search(line):
                hits.append(Hit("", line_number, "denylisted term"))
    return hits


def _denylist_path() -> Path:
    configured = os.environ.get("CLAWDOT_PRIVATE_DENYLIST")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "clawdot" / "private-denylist.txt"


def _report(hits: Iterable[Hit], fallback_path: str) -> int:
    found = list(hits)
    if not found:
        return 0
    for hit in found:
        print(f"{hit.path or fallback_path}:{hit.line}: {hit.kind}")
    print(
        "Remove the value, use a placeholder, or add `private-identifier: ok` for a deliberate "
        "generic example. "
        "Use `git commit --no-verify` only when you intentionally need to bypass this check."
    )
    return 1


def run_pre_commit(
    cwd: str | Path | None = None,
    denylist_terms: Iterable[str] | None = None,
) -> int:
    git = shutil.which("git")
    if git is None:
        print("private identifier check could not find git", file=sys.stderr)
        return 1
    result = subprocess.run(  # noqa: S603
        [git, "diff", "--cached", "-U0", "--no-color"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        print("private identifier check could not read the staged diff", file=sys.stderr)
        return 1
    terms = load_denylist(_denylist_path()) if denylist_terms is None else list(denylist_terms)
    return _report(scan_added_diff(result.stdout, terms), "staged diff")


def run_commit_message(
    path: str | Path,
    denylist_terms: Iterable[str] | None = None,
) -> int:
    try:
        message = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"private identifier check could not read commit message ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    terms = load_denylist(_denylist_path()) if denylist_terms is None else list(denylist_terms)
    hits = scan_commit_message(message, terms)
    return _report(hits, str(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--commit-msg", action="store_true")
    parser.add_argument("filenames", nargs="*")
    args = parser.parse_args(argv)
    if args.staged:
        return run_pre_commit()
    if len(args.filenames) != 1:
        parser.error("commit-message mode requires exactly one message filename")
    return run_commit_message(args.filenames[0])


if __name__ == "__main__":
    raise SystemExit(main())
