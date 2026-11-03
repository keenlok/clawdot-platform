"""Validated, read-only presentation data for release approval cards."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

MAX_PATCH_BYTES = 384 * 1024
MAX_PRESENTATION_JSON_BYTES = 768 * 1024
_URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://\S+|www\.\S+|t\.me/\S+)")


class CardIssueError(RuntimeError):
    _CODES = frozenset(
        {"invalid_card", "send_uncertain", "ledger_unavailable", "protection_unavailable"}
    )

    def __init__(self, code: str) -> None:
        self.code = code if code in self._CODES else "invalid_card"
        super().__init__(self.code)


def clean_untrusted(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise CardIssueError("invalid_card")
    cleaned = "".join(
        " "
        if unicodedata.category(c).startswith("C") or unicodedata.category(c) in {"Zl", "Zp"}
        else c
        for c in value
    )
    cleaned = _URL.sub("[link removed]", " ".join(cleaned.split()))
    return cleaned if len(cleaned) <= maximum else cleaned[: max(0, maximum - 1)] + "…"


@dataclass(frozen=True)
class FileStat:
    path: str
    added: int | None
    deleted: int | None


@dataclass(frozen=True)
class CheckLine:
    name: str
    conclusion: str
    detail: str | None = None


@dataclass(frozen=True)
class CardPresentation:
    pr_url: str
    pr_title: str = ""
    what_changes: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    file_stats: tuple[FileStat, ...] = ()
    checks: tuple[CheckLine, ...] = ()
    review_findings: int | None = None
    review_fixed: int | None = None
    patch_state: str = "unavailable"
    patch: str | None = None
    redacted_files: int = 0

    @classmethod
    def from_json(cls, value: object, binding: object) -> CardPresentation:
        try:
            if not isinstance(value, Mapping):
                raise ValueError
            allowed = {
                "pr_url",
                "pr_title",
                "what_changes",
                "risks",
                "file_stats",
                "checks",
                "review_findings",
                "review_fixed",
                "patch_state",
                "patch",
                "redacted_files",
            }
            if set(value) - allowed or not isinstance(value.get("pr_url"), str):
                raise ValueError
            n = binding.pr_number
            if (
                re.fullmatch(
                    rf"https://github\.com/[A-Za-z0-9_.-]{{1,100}}/[A-Za-z0-9_.-]{{1,100}}/pull/{n}",
                    value["pr_url"],
                )
                is None
            ):
                raise ValueError
            title = clean_untrusted(value.get("pr_title", ""), maximum=100)

            def strings(key: str, maximum_count: int) -> tuple[str, ...]:
                raw = value.get(key, [])
                if (
                    not isinstance(raw, list)
                    or len(raw) > maximum_count
                    or any(not isinstance(s, str) for s in raw)
                ):
                    raise ValueError
                return tuple(clean_untrusted(s, maximum=160) for s in raw)

            changes, risks = strings("what_changes", 5), strings("risks", 4)
            raw_stats = value.get("file_stats", [])
            if not isinstance(raw_stats, list):
                raise ValueError
            stats = tuple(
                FileStat(s["path"], s["added"], s["deleted"])
                for s in raw_stats
                if isinstance(s, Mapping) and set(s) == {"path", "added", "deleted"}
            )
            if len(stats) != len(raw_stats):
                raise ValueError
            expected = [c["path"] for c in binding.review_bundle["deployed_to_head"]]
            paths = [s.path for s in stats]
            if len(set(paths)) != len(paths) or set(paths) != set(expected):
                raise ValueError
            for s in stats:
                if (
                    not isinstance(s.path, str)
                    or any(
                        x is not None and (type(x) is not int or not 0 <= x <= 10_000_000)
                        for x in (s.added, s.deleted)
                    )
                    or ((s.added is None) != (s.deleted is None))
                ):
                    raise ValueError
            raw_checks = value.get("checks", [])
            if not isinstance(raw_checks, list) or len(raw_checks) > 8:
                raise ValueError
            checks = tuple(
                CheckLine(c["name"], c["conclusion"], c["detail"])
                for c in raw_checks
                if isinstance(c, Mapping) and set(c) == {"name", "conclusion", "detail"}
            )
            if len(checks) != len(raw_checks) or any(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _./-]{0,39}", c.name) is None
                or c.conclusion
                not in {"success", "failure", "skipped", "neutral", "cancelled", "timed_out"}
                or (
                    c.detail is not None
                    and (
                        not isinstance(c.detail, str)
                        or re.fullmatch(r"[A-Za-z0-9 .,:/+-]{0,40}", c.detail) is None
                    )
                )
                for c in checks
            ):
                raise ValueError
            findings, fixed = value.get("review_findings"), value.get("review_fixed")
            if (findings is None) != (fixed is None) or (
                findings is not None
                and (
                    type(findings) is not int
                    or type(fixed) is not int
                    or not 0 <= fixed <= findings <= 1000
                )
            ):
                raise ValueError
            state, patch, redacted = (
                value.get("patch_state"),
                value.get("patch"),
                value.get("redacted_files", 0),
            )
            if (
                state not in {"included", "too_large", "unavailable"}
                or (not isinstance(patch, str) if state == "included" else patch is not None)
                or type(redacted) is not int
                or redacted < 0
                or (
                    isinstance(patch, str)
                    and (len(patch.encode()) > MAX_PATCH_BYTES or "\0" in patch)
                )
            ):
                raise ValueError
            return cls(
                value["pr_url"],
                title,
                changes,
                risks,
                stats,
                checks,
                findings,
                fixed,
                state,
                patch,
                redacted,
            )
        except Exception:
            raise CardIssueError("invalid_card") from None

    def to_json(self) -> dict[str, object]:
        return {
            "pr_url": self.pr_url,
            "pr_title": self.pr_title,
            "what_changes": list(self.what_changes),
            "risks": list(self.risks),
            "file_stats": [
                {"path": s.path, "added": s.added, "deleted": s.deleted} for s in self.file_stats
            ],
            "checks": [
                {"name": c.name, "conclusion": c.conclusion, "detail": c.detail}
                for c in self.checks
            ],
            "review_findings": self.review_findings,
            "review_fixed": self.review_fixed,
            "patch_state": self.patch_state,
            "redacted_files": self.redacted_files,
        }


def group_key(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 and parts[0] in {"packages", "apps"} else path


def touches(file_stats: tuple[FileStat, ...] | list[FileStat]) -> list[dict[str, object]]:
    grouped: dict[str, list[FileStat]] = defaultdict(list)
    for stat in file_stats:
        grouped[group_key(stat.path)].append(stat)
    return sorted(
        (
            {
                "key": k,
                "added": sum(s.added or 0 for s in v),
                "deleted": sum(s.deleted or 0 for s in v),
                "files": len(v),
                "has_binary": any(s.added is None for s in v),
                "paths": tuple(s.path for s in v),
            }
            for k, v in grouped.items()
        ),
        key=lambda g: (-(int(g["added"]) + int(g["deleted"])), str(g["key"])),
    )


# The pre-move path stays listed so diffs spanning the move still raise the flag.
_RELEASE_CONTROL_PREFIXES = ("services/release-control/", "packages/plugin-release-control/")


def computed_flags(paths: list[str] | tuple[str, ...]) -> dict[str, int]:
    result = {"migrations": 0, "config": 0, "deploy_ci": 0, "release_control": 0}
    for path in set(paths):
        parts = path.split("/")
        base = parts[-1]
        if ("migrations" in parts and base.endswith(".sql")) or base.endswith("schema.sql"):
            result["migrations"] += 1
        if base in {"pyproject.toml", "uv.lock", ".env.example"} or "config" in parts:
            result["config"] += 1
        if (
            ".github" in parts
            or "deploy" in parts
            or base.startswith("Dockerfile")
            or re.fullmatch(r"compose.*\.ya?ml", base)
        ):
            result["deploy_ci"] += 1
        if path.startswith(_RELEASE_CONTROL_PREFIXES):
            result["release_control"] += 1
    return result


def render_flags(flags: Mapping[str, int]) -> str:
    def part(key: str, label: str) -> str:
        count = flags.get(key, 0)
        return (
            f"⚠️ {label} ({count} files)"
            if key == "migrations" and count
            else f"⚠️ {label} ({count})"
            if count
            else "No migrations"
            if key == "migrations"
            else f"no {label}"
        )

    value = " · ".join(
        (
            part("migrations", "Migrations"),
            part("config", "config"),
            part("deploy_ci", "deploy/CI files"),
        )
    )
    if flags.get("release_control", 0):
        value += f" · ⚠️ release-control files ({flags['release_control']})"
    return value


def find_contradictions(
    lines: list[str] | tuple[str, ...], flags: Mapping[str, int]
) -> list[tuple[int, str]]:
    patterns = (
        (
            "migrations",
            re.compile(
                r"\bno (?:db |database |schema )?(?:migrations?|schema changes?|db changes?|"
                r"database changes?)\b",
                re.I,
            ),
            "migration files changed",
        ),
        (
            "config",
            re.compile(r"\bno config(?:uration)?(?: changes?)?\b", re.I),
            "configuration files changed",
        ),
        (
            "deploy_ci",
            re.compile(
                r"\bno (?:deploy(?:ment)?|ci|workflow)(?: files?| changes?)?\b",
                re.I,
            ),
            "deploy/CI files changed",
        ),
        (
            "release_control",
            re.compile(r"\bno release[- ]control(?: files?| changes?)?\b", re.I),
            "release-control files changed",
        ),
    )
    found = []
    for i, line in enumerate(lines):
        for key, pattern, wording in patterns:
            if flags.get(key, 0) and pattern.search(line):
                found.append(
                    (i, f'⚠️ PR text says "{pattern.search(line).group(0).lower()}", but {wording}')
                )
                break
    return found


def parse_pr_summary(body: str) -> tuple[list[str], list[str]]:
    if not isinstance(body, str):
        return [], []
    what: list[str] = []
    risks: list[str] = []
    section = ""
    fallback = []
    for raw in body.splitlines():
        line = raw.strip()
        heading = re.sub(r"^#+\s*|:\s*$", "", line).strip().lower()
        if re.fullmatch(r"(?:what changes|summary|changes)", heading):
            section = "what"
            continue
        if re.fullmatch(r"(?:risks?|limits|risks?\s*/\s*limits|known limitations)", heading):
            section = "risks"
            continue
        if not line:
            continue
        if line.startswith("#"):
            section = ""
            continue
        match = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", line)
        if match and section in {"what", "risks"}:
            (what if section == "what" else risks).append(
                clean_untrusted(match.group(1), maximum=160)
            )
        elif not match and not re.search(r"\b(?:changes|summary|risks?|limits)\s*:$", line, re.I):
            fallback.append(line)
    if not what:
        what = [clean_untrusted(x, maximum=160) for x in fallback[:3]]
    return what[:5], risks[:4]


def redact_patch(patch: bytes, scanner: object, allowed_paths: frozenset[str]) -> tuple[str, int]:
    chunks = re.split(rb"(?m)^(?=diff --git )", patch)
    result = []
    redacted = 0
    for index, section in enumerate(chunks):
        if not section:
            continue
        match = re.match(rb"diff --git a/(.*?) b/(.*?)\n", section, re.S)
        path = match.group(2).decode("utf-8", "replace") if match else ""
        clean = False
        try:
            from .publication import ScanInput, ScanVerdict

            clean = (
                b"\0" not in section
                and section.decode("utf-8") is not None
                and scanner(ScanInput(f"release_diff:{index}", section)) is ScanVerdict.CLEAN
            )
        except Exception:
            clean = False
        if clean:
            result.append(section.decode("utf-8"))
        else:
            redacted += 1
            shown = path if path in allowed_paths else "[redacted section]"
            result.append(
                f"diff --git a/{shown} b/{shown}\n"
                "# [redacted: the secret scanner flagged this file's changes]\n"
            )
    return "".join(result), redacted


def build_card_presentation(
    repository,
    binding,
    *,
    pr_url,
    pr_title,
    pr_body,
    checks=(),
    review_findings=None,
    review_fixed=None,
    scanner,
):
    from .git_evidence import collect_delta_patch, collect_delta_stats

    older, newer = binding.deployed_identity.git_sha, binding.head_sha
    stats = collect_delta_stats(repository, older, newer)
    state, patch_text, redacted = "unavailable", None, 0
    try:
        raw = collect_delta_patch(repository, older, newer, max_bytes=MAX_PATCH_BYTES)
        if raw is None:
            state = "too_large"
        else:
            patch_text, redacted = redact_patch(raw, scanner, frozenset(p for p, _, _ in stats))
            if len(patch_text.encode("utf-8")) > MAX_PATCH_BYTES:
                patch_text, state = None, "too_large"
            else:
                state = "included"
    except Exception:
        state = "unavailable"
    what, risks = parse_pr_summary(pr_body)
    data = {
        "pr_url": pr_url,
        "pr_title": pr_title,
        "what_changes": what,
        "risks": risks,
        "file_stats": [{"path": p, "added": a, "deleted": d} for p, a, d in stats],
        "checks": list(checks),
        "review_findings": review_findings,
        "review_fixed": review_fixed,
        "patch_state": state,
        "patch": patch_text,
        "redacted_files": redacted,
    }
    if (
        len(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
        > MAX_PRESENTATION_JSON_BYTES
    ):
        # JSON escaping can inflate a patch past the bridge's request limit.
        data.update(patch_state="too_large", patch=None)
    CardPresentation.from_json(data, binding)
    return data


def render_details(binding_json: Mapping) -> list[str]:
    from .telegram_cards import _display_path

    review = binding_json["review_bundle"]
    identity = binding_json["deployed_identity"]
    lines = [
        f"PR #{binding_json['pr_number']} → {binding_json['target']}",
        f"Release kind: {review.get('release_kind', 'ordinary')}",
        f"D commit: {identity['git_sha']}",
        f"D tree: {review['deployed_tree']}",
        f"Image digest: {identity['image_digest']}",
        f"Config generation: {identity['config_generation']}",
        f"Config digest: {identity['config_digest']}",
        f"Target sequence: {identity['target_sequence']}",
        f"B commit: {binding_json['base_sha']}",
        f"B tree: {review['base_tree']}",
        "B is an ancestor of H: verified",
        f"H commit: {binding_json['head_sha']}",
        f"H tree: {binding_json['head_tree']}",
        f"Policy digest: {binding_json['policy_digest']}",
        f"Trusted check evidence digest: {review['check_evidence_digest']}",
    ]
    path_count = 0
    for title, key in (
        ("D → B inherited", "deployed_to_base"),
        ("B → H PR changes", "base_to_head"),
        ("Complete D → H delta", "deployed_to_head"),
    ):
        lines.append(title + ":")
        lines.extend(_display_path(item) for item in review[key])
        path_count += len(review[key])
    return _pack_messages(lines, path_lines=path_count)


_DETAIL_HEADINGS = frozenset({"D → B inherited:", "B → H PR changes:", "Complete D → H delta:"})


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _pack_messages(lines: list[str], *, path_lines: int, limit: int = 4096) -> list[str]:
    """Greedy-pack lines into at most five messages, ending with an omission tail if needed."""
    messages: list[list[str]] = [[]]
    sizes = [0]
    placed_paths = 0
    path_section = False
    tail_reserve = _utf16(f"… and {path_lines} more paths (full list in View diff)") + 1
    for line in lines:
        path_section = path_section or line in _DETAIL_HEADINGS
        extra = _utf16(line) + (1 if messages[-1] else 0)
        if sizes[-1] + extra > limit - (tail_reserve if len(messages) == 5 else 0):
            if len(messages) == 5:
                break
            messages.append([])
            sizes.append(0)
            extra = _utf16(line)
            if extra > limit - (tail_reserve if len(messages) == 5 else 0):
                break
        messages[-1].append(line)
        sizes[-1] += extra
        if path_section and line not in _DETAIL_HEADINGS:
            placed_paths += 1
    omitted = path_lines - placed_paths
    if omitted > 0:
        messages[-1].append(f"… and {omitted} more paths (full list in View diff)")
    return ["\n".join(message) for message in messages if message]


class ReleaseCardViewStore:
    def __init__(self, pool):
        self._pool = pool

    async def ready(self):
        return (
            await self._pool.fetchval(
                "SELECT to_regclass('release_control.release_card_view') IS NOT NULL"
            )
            is True
        )

    async def put(self, approval_id, presentation_json, patch, patch_state):
        await self._pool.execute(
            "INSERT INTO release_control.release_card_view(approval_id, presentation, patch, "
            "patch_state) "
            "VALUES($1,$2::jsonb,$3,$4) ON CONFLICT DO NOTHING",
            approval_id,
            json.dumps(presentation_json),
            patch,
            patch_state,
        )

    async def get(self, approval_id):
        row = await self._pool.fetchrow(
            "SELECT presentation,patch,patch_state FROM release_control.release_card_view "
            "WHERE approval_id=$1",
            approval_id,
        )
        if row is None:
            return None
        return {
            "presentation": json.loads(row["presentation"])
            if isinstance(row["presentation"], str)
            else row["presentation"],
            "patch": bytes(row["patch"]) if row["patch"] is not None else None,
            "patch_state": row["patch_state"],
        }
