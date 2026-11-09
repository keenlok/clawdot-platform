"""The private identifier hook reports locations and kinds, never matched values."""

import runpy
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / ".github/scripts/check_private_identifiers.py"))


def test_real_looking_telegram_ids_and_links_are_reported_without_values(capsys):
    chat_id = "tg:-100" + "4829175036"
    user_id = "tg:" + "58203947"
    link = "t.me/" + "examplegroupxyz"
    https_link = "https://" + link
    hits = API["scan_text"](f"{chat_id} {user_id} {link}")

    assert [hit.kind for hit in hits] == [
        "telegram chat id",
        "telegram user id",
        "telegram link",
    ]
    assert all(hit.line == 1 for hit in hits)
    captured = capsys.readouterr()
    assert chat_id not in captured.out + captured.err
    assert user_id not in captured.out + captured.err
    assert link not in captured.out + captured.err
    assert [hit.kind for hit in API["scan_text"](https_link)] == ["telegram link"]


@pytest.mark.parametrize(
    "value",
    [
        "tg:123456789",
        "tg:-1001234567890",
        "tg:" + "1" * 7,
        "t.me/BotFather",
        "t.me/addstickers",
    ],
)
def test_placeholder_ids_and_service_links_are_allowed(value):
    assert API["scan_text"](value) == []


def test_documentation_marker_skips_a_line():
    chat_id = "tg:-100" + "4829175036"
    assert API["scan_text"](f"Example {chat_id} private-identifier: ok") == []


def test_denylist_matches_case_insensitively_with_optional_at_and_whole_words():
    hits = API["scan_text"](
        "@Alice Bob HDMI",
        denylist_terms=["Alice", "@Bob", "HD"],
    )

    assert [hit.kind for hit in hits] == ["denylisted term", "denylisted term"]


def test_denylist_file_ignores_comments_and_blanks_and_missing_file(tmp_path):
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("# private terms\n\n@Alice\nBob\n", encoding="utf-8")

    assert API["load_denylist"](denylist) == ["Alice", "Bob"]
    assert API["load_denylist"](tmp_path / "missing.txt") == []


def test_unreadable_denylist_warns_without_failing(tmp_path, monkeypatch, capsys):
    def unreadable(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", unreadable)

    assert API["load_denylist"](tmp_path / "private.txt") == []
    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert "private.txt" not in captured.err


def test_commit_message_ignores_comments_and_coauthor_trailers():
    message = """Subject

# Alice is only in a comment
Co-Authored-By: Alice <alice@example.invalid>
Body mentions Bob
"""

    hits = API["scan_commit_message"](message, denylist_terms=["Alice", "Bob"])
    assert [(hit.line, hit.kind) for hit in hits] == [(5, "denylisted term")]


def test_added_diff_lines_only_are_scanned_and_header_is_not():
    chat_id = "tg:-100" + "4829175036"
    user_id = "tg:" + "58203947"
    link = "t.me/" + "examplegroupxyz"
    diff = f"""diff --git a/example.txt b/example.txt
--- a/example.txt
+++ b/example.txt
@@ -1,2 +1,4 @@
-{chat_id}
 context {chat_id}
+{chat_id}
+{user_id}
+{link}
"""

    hits = API["scan_added_diff"](diff, denylist_terms=[])
    assert [(hit.path, hit.line, hit.kind) for hit in hits] == [
        ("example.txt", 2, "telegram chat id"),
        ("example.txt", 3, "telegram user id"),
        ("example.txt", 4, "telegram link"),
    ]


def test_pre_commit_mode_scans_staged_added_lines_in_temporary_repo(tmp_path, capsys):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "main")
    git("config", "user.name", "Placeholder")
    git("config", "user.email", "placeholder@example.invalid")
    target = tmp_path / "note.txt"
    target.write_text("safe\n", encoding="utf-8")
    git("add", "note.txt")
    git("commit", "-m", "base")
    chat_id = "tg:-100" + "4829175036"
    target.write_text(f"safe\n{chat_id}\n", encoding="utf-8")
    git("add", "note.txt")

    result = API["run_pre_commit"](cwd=tmp_path, denylist_terms=[])

    assert result == 1
    captured = capsys.readouterr()
    assert "note.txt:2: telegram chat id" in captured.out
    assert chat_id not in captured.out + captured.err


def test_commit_message_output_does_not_reveal_the_matched_term(tmp_path, capsys):
    message = tmp_path / "COMMIT_EDITMSG"
    private_term = "Zorblax"
    message.write_text(f"Release {private_term}\n", encoding="utf-8")

    result = API["run_commit_message"](
        message,
        denylist_terms=[private_term],
    )

    captured = capsys.readouterr()
    assert result == 1
    assert f"{message}:1: denylisted term" in captured.out
    assert private_term not in captured.out + captured.err


def test_main_staged_dispatch_handles_multiple_staged_files(tmp_path, monkeypatch, capsys):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "main")
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("safe\n", encoding="utf-8")
    second.write_text("also safe\n", encoding="utf-8")
    git("add", "first.txt", "second.txt")
    git("commit", "-m", "base")
    first.write_text("safe\n", encoding="utf-8")
    private_term = "Zorblax"
    second.write_text(f"{private_term}\n", encoding="utf-8")
    git("add", "first.txt", "second.txt")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(f"{private_term}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAWDOT_PRIVATE_DENYLIST", str(denylist))

    result = API["main"](["--staged", "first.txt", "second.txt"])

    captured = capsys.readouterr()
    assert result == 1
    assert "second.txt:1: denylisted term" in captured.out
    assert private_term not in captured.out + captured.err


def test_main_commit_message_dispatch_scans_message_not_staged_diff(tmp_path, monkeypatch, capsys):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "main")
    target = tmp_path / "note.txt"
    target.write_text("safe\n", encoding="utf-8")
    git("add", "note.txt")
    git("commit", "-m", "base")
    target.write_text("safe\n", encoding="utf-8")
    git("add", "note.txt")
    private_term = "Zorblax"
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(f"{private_term}\n", encoding="utf-8")
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(f"Mention {private_term}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAWDOT_PRIVATE_DENYLIST", str(denylist))

    result = API["main"](["--commit-msg", str(message)])

    captured = capsys.readouterr()
    assert result == 1
    assert f"{message}:1: denylisted term" in captured.out
    assert "staged diff" not in captured.out
    assert private_term not in captured.out + captured.err


def test_pre_commit_config_separates_staged_and_commit_message_hooks():
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = {
        hook["id"]: hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook["id"].startswith("private-identifiers")
    }

    assert hooks["private-identifiers"]["stages"] == ["pre-commit"]
    assert hooks["private-identifiers"]["args"] == ["--staged"]
    assert hooks["private-identifiers"]["pass_filenames"] is False
    assert hooks["private-identifiers"]["always_run"] is True
    assert hooks["private-identifiers"]["language"] == "python"
    assert hooks["private-identifiers-commit-msg"]["stages"] == ["commit-msg"]
    assert hooks["private-identifiers-commit-msg"]["args"] == ["--commit-msg"]
    assert hooks["private-identifiers-commit-msg"]["language"] == "python"
