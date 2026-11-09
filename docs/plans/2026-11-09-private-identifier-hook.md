# Private identifier pre-commit check

Status: Live limited. Approved by the owner on 2026-11-09; delivered in [PR #27](https://github.com/keenlok/clawdot-platform/pull/27). Maintainers opt in with `uv run pre-commit install`.

## Why

This repository is public. On 2026-11-09 a commit message and plan file almost carried real
group members' names, Telegram handles and context from a production chat into public
history. Only an outside permission check stopped the push. gitleaks (already in
`.pre-commit-config.yaml`) looks for secrets, not people.

The names to catch can't be listed in the repository, or the list itself leaks them. So the
check has two parts:

1. **Generic patterns, in the repository**: safe to publish, apply to every contributor.
2. **A personal denylist, outside the repository**: each maintainer's own file of real
   names and handles, read only if it exists.

## Design

New script `.github/scripts/check_private_identifiers.py` (standard library only), wired as
a `repo: local` hook in `.pre-commit-config.yaml` for two stages:

- `pre-commit`: scans **added lines only** of the staged diff (`git diff --cached -U0
  --no-color`), so existing content never blocks a commit.
- `commit-msg`: scans the commit message file (ignoring `#` comment lines and trailers such
  as `Co-Authored-By:`).

Add `default_install_hook_types: [pre-commit, commit-msg]` so `uv run pre-commit install`
installs both.

### Generic patterns (always on)

- Telegram chat keys with a real-looking group ID: `tg:-100` followed by 10+ digits.
- Raw Telegram user keys: `tg:` followed by 7+ digits.
- Telegram links to a specific user or group: `t.me/<name>` (5+ chars), excluding
  `t.me/BotFather` and `t.me/addstickers`-style service paths listed in the script.

Placeholders are allowed: an ID whose digits are an ascending run from `1` (for example
`123456789`, `1001234567890`), or a single repeated digit. A line containing the marker
`private-identifier: ok` is skipped (for documentation that must show a real-looking
example).

### Personal denylist (optional)

- Path: `$CLAWDOT_PRIVATE_DENYLIST`, else `~/.config/clawdot/private-denylist.txt`.
- One term per line; blank lines and `#` comments ignored; a leading `@` is optional and
  matched either way.
- Case-insensitive, whole-word match (word boundaries, Unicode-aware), so a short name
  like `HD` doesn't match `HDMI`.
- Missing file → this part is skipped silently. Unreadable file → warning, not failure.

### Output

On a hit: exit 1, print `path:line: <kind>` per hit, where kind is `telegram chat id`,
`telegram user id`, `telegram link`, or `denylisted term`. **Do not print the matched
text** (the output can end up in terminal scrollback, CI logs or chat transcripts). Print a
one-line hint: remove it, use a placeholder, or add `private-identifier: ok` for a
deliberate generic example. Never log the denylist path's contents.

`git commit --no-verify` remains the escape hatch; the hint mentions it.

## Owned files

- `.github/scripts/check_private_identifiers.py` (new)
- `.pre-commit-config.yaml`
- `tests/test_private_identifiers.py` (new)
- `AGENTS.md`: one line under "Working rules" next to the gitleaks rule
- `CHANGELOG.md`: `Unreleased` → `Added`, a maintainer-facing entry
- `docs/plans/README.md`: this plan's row (already added)

No CI workflow change in this PR; running the generic check in CI is a possible follow-up.

## Tests

Pure unit tests calling the script's functions on strings and temporary files (no git
repository needed except one test that builds a temporary repo, stages a file and runs the
pre-commit mode end to end):

- Real-looking chat ID, user ID and `t.me/` link in an added line → hit with the right kind.
- Placeholder IDs (`tg:123456789`, `tg:-1001234567890`, `tg:1111111`) → no hit.
- `private-identifier: ok` marker → no hit.
- Removed and context lines never hit; only `+` lines (not the `+++` header).
- Denylist: case-insensitive, `@` optional, whole-word only (`HD` vs `HDMI`), comments and
  blanks ignored; missing file → no hit, no error.
- Commit message mode skips `#` lines and `Co-Authored-By:` trailers.
- Output never contains the matched text (assert the term is absent from captured stdout
  and stderr).

## Checks

```sh
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync --locked --all-packages
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check <changed .py files>
uv run pytest -q tests/test_private_identifiers.py tests/test_plans_index.py
```

## Done when

- `uv run pre-commit install` installs both hooks, and a commit adding a real-looking ID or
  a denylisted term is refused without echoing the term.
- Existing files on `main` don't cause failures (only added lines are scanned).
- Tests above pass; lint clean.

## Execution

Claude planned; Codex executes and reviews with `gpt-6-luna` at high reasoning effort
(owner's request, 2026-11-09); Claude does the final review.
