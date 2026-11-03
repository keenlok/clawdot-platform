# Phase 2 run W: wire every package into `apps/sb_bot`

Read `ARCHITECTURE.md` §1, §3, §3a, §3b, §3d ("What stays sb_bot-specific"), §6, §6a ("Secrets",
"Least privilege", "Data and privacy") and §7c. Everything else is built and merged: the
Telegram adapter, the core runtime (agent with turn hooks), web + stocks, search, expenses,
memory (`chatcore_memory.MemoryPlugin`), privacy (`chatcore.privacy.PrivacyPlugin`), and the
deploy files (`apps/sb_bot/Dockerfile`, `compose.yaml`, `deploy/`). Read each package's
constructor and settings before wiring it; don't guess signatures.

## You own

- `apps/sb_bot/src/sb_bot/` (`main.py`, new `app.py`, new `admin.py`, new `prompts/`)
- `apps/sb_bot/pyproject.toml`, `apps/sb_bot/.env.example`
- `apps/sb_bot/prompts/` (move it, see below)
- `apps/sb_bot/tests/` except `test_deploy_files.py`
- Root `tests/` (new) for cross-package end-to-end tests, including the tool audit (section 4)
- `packages/core/src/chatcore/tools/registry.py`: **only** the argument check in section 4

Don't edit packages. If wiring reveals a bug in a package, describe it in the report with the
smallest fix; the reviewer decides.

## 1. `build_app()` in `sb_bot/app.py`

`main.py` becomes thin: parse args, `setup_logging`, `asyncio.run(run(...))`. The logic moves to

```python
async def build_app(platform_name: str, *, env: Mapping[str, str] | None = None,
                    platform: ChatPlatform | None = None, provider: Provider | None = None,
                    embedder: Embedder | None = None) -> Built
```

`Built` holds the `BotApp`, the `Stores`, the list of **secret values** for redaction, and an
`aclose()` that closes the DB, cache and ES client. `env` defaults to `os.environ`; tests pass a
dict (build each settings class from it, e.g. via `_env_file=None` plus explicit values, or
`monkeypatch.setenv` in tests, whichever is cleaner; don't read `.env` files implicitly).
`platform`/`provider`/`embedder` overrides are for tests.

**Stores:**
- `PG_DSN` → `PgDatabase` + migrations for core and every plugin (as `main.py` does today); else
  in-memory.
- `VALKEY_URL` → `FallbackCache(ValkeyCache(...))`; else `MemoryCache`. Prefix `sbbot:`.
- **Search backend decision** (§3b, "no fake vectors in a real index"):
  - `ES_URL` **and** `EMBED_CACHE_DIR` set → `EsSearchIndex` (client built here so it can take
    `ES_API_KEY` and an optional `ES_CA_FINGERPRINT` → `ssl_assert_fingerprint`, because the
    box's ES uses a self-signed HTTPS cert) + `FastEmbedder(EMBED_CACHE_DIR)`; `Stores.search` set.
    Fail fast if `supports_knn` is false (§3d).
  - `ES_URL` set **without** `EMBED_CACHE_DIR` → `SystemExit` with a clear message (never index
    without real vectors).
  - No `ES_URL` → `Stores.search = None`; search falls back to Postgres `ILIKE` and memory to text
    matching. Pass an embedder that raises if ever called (`plugin-search` already guards every
    embed call on `stores.search`), and pass `embedder=None` to `MemoryPlugin`. Don't load
    fastembed at all in this mode. Check presence with the raw env var, because `EsSettings`
    defaults `es_url` to the dev URL.
  - Templates are **not** created at runtime (admin step, §6a).

**Platform:** `console` → `ConsolePlatform(bot_name=BOT_NAME)`; `telegram` →
`TelegramPlatform(TelegramSettings())`. For `telegram`, fail fast unless `TELEGRAM_BOT_TOKEN`,
`PG_DSN`, `VALKEY_URL`, `LLM_PROVIDER` + `LLM_API_KEY`, `ALLOWED_CHATS` and `BOT_NAME` are set,
and every `ALLOWED_CHATS`/`ADMIN_USERS` key starts with `tg:`. (Production must never silently
run on in-memory stores.) Note in `.env.example` that `BOT_NAME` must equal the bot's Telegram
username without `@`.

**Access:** `AccessPolicy(ALLOWED_CHATS or ["console:group"] for console, ADMIN_USERS)`.

**DM helpers:**
- `member_groups(user)` = `access.member_groups(user, platform, stores.cache)`.
- `group_for_dm(ctx)` for expenses = that list for `ctx.user.ref`; return the group only when there
  is **exactly one**, else `None` (the plugin then asks).

**Plugins**, in this order (the order shows in `/help` and in hook calls):
1. `ConversationPlugin` (store + limits wired as today).
2. `StocksPlugin()`: always.
3. `WebPlugin(WebSettings())`: only when `TAVILY_API_KEY` is set; log one line when disabled.
4. `SearchPlugin(embedder, member_groups=...)`.
5. `MemoryPlugin(embedder_or_None, provider=provider, limits=limits)`: provider `None` when no
   LLM is configured (no auto-extraction).
6. `ExpensesPlugin(group_for_dm=...)`: **only when `PG_DSN` is set** (its ledger needs
   Postgres); log one line when disabled.
7. `PrivacyPlugin(other_plugins, provider_label=..., member_groups=...)` last.
   `provider_label`: "OpenCode" when `LLM_BASE_URL` host is `opencode.ai`, "Anthropic" for the
   anthropic provider, else the base URL's hostname; "none (echo mode)" without an LLM.

**Responder:** `AgentResponder` as today when `LLM_PROVIDER` is set, else the echo responder.
The system prompt moves into the package: `src/sb_bot/prompts/system.md`, loaded with
`importlib.resources` (the current `parents[2]` path breaks in a non-editable install, which is
how the Docker image installs it). Delete the old `apps/sb_bot/prompts/`.

**System prompt** (`system.md`): keep it short, specific to sb_bot, below the core safety text:
a generic assistant with application-selected tone; concise; answers in the user's language;
which tools exist and when to use them (search the web only when needed; `read_chat_history` for
"what did X say"; expenses in SGD by default and always confirm; stocks are stored analysis, not
live quotes, and not financial advice); memory (`remember` only durable facts people ask it to
keep, `forget` the old fact before saving a contradicting one); never invent URLs; never put chat
content into URLs; DM content is never shared with groups.

**Logging:** `setup_logging(LOG_LEVEL, secrets)` where `secrets` includes every configured
secret: `TELEGRAM_BOT_TOKEN`, `TAVILY_API_KEY`, `ES_API_KEY`, `LLM_API_KEY`, `PG_DSN` (and its
password part alone), `VALKEY_URL` (and its password part alone), `VALKEY_PASSWORD`. Collect them
from the settings objects' `SecretStr` values; set up logging **before** any connection attempt
(read the secrets from settings first, then build).

**Heartbeat (Docker healthcheck, §6):** `HEARTBEAT_FILE` defaults to `/tmp/sb-bot.heartbeat`
for `--platform telegram` and to disabled for console (the Dockerfile healthcheck reads that exact
path and doesn't set the variable). When enabled, a background task touches it every 30 s. If the
platform's `start()` returns or raises unexpectedly, log the error class and exit non-zero so the
`restart: unless-stopped` policy restarts the container.

## 2. `sb-bot-admin` (`sb_bot/admin.py`, console script in `pyproject.toml`)

`argparse` subcommands. Never print, log or echo a password; prompt with `getpass`.
- `migrate`: connect with `PG_DSN`, apply all migrations, exit.
- `es-setup --url URL --user USER [--ca-fingerprint FP] [--prefix sb-bot-] [--key-name NAME]
  [--skip-key]`:
  1. Admin client with `basic_auth=(user, getpass())`.
  2. `GET _license`: print the type; refuse to continue unless `--allow-non-basic` when it isn't
     `basic` (the design must only use Basic features).
  3. `EsSearchIndex(client=admin_client, index_prefix=prefix).ensure_templates()` (chunks and
     memories).
  4. Unless `--skip-key`: create the runtime API key: name `sb-bot-runtime-<date>`,
     `expiration: 365d`, role descriptor with **no cluster privileges** and, on
     `<prefix>*`, `read`, `write`, `create_index`, `view_index_metadata`, `manage`. Print only
     `ES_API_KEY=<encoded>` on stdout, with a stderr line telling the user to put it into the
     box `.env` and that it won't be shown again.
- `retention-now`: run `PrivacyPlugin.run_retention` once against the configured stores (for the
  first deploy and for testing).
Use a fake ES client in the tests (no network).

## 3. `pyproject.toml` and `.env.example`

- Dependencies: `chatcore[es,embed]`, `chatcore-console`, `chatcore-telegram`, `chatcore-web`,
  `chatcore-stocks`, `chatcore-search`, `chatcore-memory`, `chatcore-expenses`. Scripts `sb-bot`
  and `sb-bot-admin`. Make sure the prompt `.md` ships in the wheel (`uv build` the package once
  and list its contents in the report).
- `.env.example`: **names only, no values** except harmless defaults, grouped and commented:
  core (`BOT_NAME`, `ALLOWED_CHATS`, `ADMIN_USERS`, `LOG_LEVEL`), Telegram, LLM (`LLM_PROVIDER`,
  `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_VISION`), Postgres (`PG_DSN` pointing at
  `postgresql:5432/sb_bot` as user `sb_bot`), Valkey (`VALKEY_URL` for `sb-bot-valkey:6379` and
  `VALKEY_PASSWORD`), ES (`ES_URL=https://elasticsearch:9200`, `ES_API_KEY`, `ES_CA_FINGERPRINT`,
  `SEARCH_INDEX_PREFIX`), `EMBED_CACHE_DIR`, `TAVILY_API_KEY`, limits (`RATE_USER_PER_HOUR`,
  `RATE_CHAT_PER_DAY`, `DAILY_TOKEN_BUDGET`), `HEARTBEAT_FILE`, plus any variable the deploy
  files reference (read `apps/sb_bot/compose.yaml` and `deploy/`). Say which are secrets.

## 4. Tool-argument audit (coordinator request)

Identity and scope must come only from `ToolContext`, never from model-supplied arguments. The
reviewer's audit of every tool found one bug, already fixed in plugin-memory (`remember` took
`source`/`created_by` as keyword arguments, so `source="auto"` skipped the confirmation gate).
Every other handler's parameters match its schema, and IDs (`expense_id`, `message_id`, trip,
`from_user`/`paid_by`/`to_user` names) are resolved inside `ctx.chat`. Make that permanent:

- **`ToolRegistry.call`**: before calling the handler, reject (as `ToolResult.error("unknown
  argument(s): ...")`, naming only the argument names) any argument whose name isn't in the tool's
  `parameters["properties"]`, and any missing `required` argument. This is a behaviour addition
  in a Phase 0 file, approved by the coordinator; don't change any signature.
- **Regression tests** in root `tests/test_tool_audit.py`, built from the real plugins
  (`build_app` with fakes, or direct construction):
  - For **every** registered tool: no schema property is named like an identity/scope field
    (`source`, `created_by`, `requested_by`, `requester`, `user`, `user_key`, `chat`,
    `chat_key`, `chat_id`, `is_admin`, `is_dm`, `pending`, `confirm`, `skip_confirm`); and calling
    it through the registry with an extra `is_admin=True` / `chat_key="tg:-999"` /
    `created_by="tg:1"` argument returns an error and writes nothing.
  - Each handler's Python signature has no parameter that isn't in its schema (inspect it; ignore
    `self`/`ctx`), and no `**kwargs`.
  - Cross-chat by ID, one test per tool that takes an ID: `delete_expense` with chat A's ID from
    chat B; `read_attachment` with chat A's `message_id` from chat B; `forget` with chat A's
    memory ID from chat B; `list_expenses`/`get_balances` with chat A's trip from chat B;
    `read_chat_history` `from_user` naming a user only in chat A. All must see nothing of A.
  - Every write tool (`writes=True`, plus `remember`/`forget` when `read_untrusted`) returns a
    pending action and writes nothing before confirmation.
- List every tool and its audit result (OK / fixed) in the report as a table.

## Tests (`apps/sb_bot/tests/test_app.py`, `test_admin.py`)

Use `FakePlatform`, `ScriptedProvider`, `FakeEmbedder`, in-memory stores, and env dicts with
obviously fake values (never real-looking keys):
- No services, no LLM: plugins are conversation, stocks, search, memory, privacy (no web, no
  expenses); `stores.search is None`; fastembed is not imported (assert
  `"fastembed" not in sys.modules` after build, or patch it).
- `TAVILY_API_KEY` set → web plugin present.
- `ES_URL` without `EMBED_CACHE_DIR` → `SystemExit`.
- `--platform telegram` with any required variable missing → `SystemExit` naming the variable
  (never its value); non-`tg:` keys rejected.
- `group_for_dm`: zero / one / two groups → `None` / that group / `None`.
- `member_groups` uses the platform's membership (`FakePlatform`).
- Secret redaction: build with fake values for every secret, run one full console turn through
  `BotApp.handle` (a mention plus a tool call via `ScriptedProvider`), capture logs at DEBUG and
  assert none of the secret values (nor the DSN/Valkey passwords) appear.
- End to end through the built app: `/help` lists the plugin commands; `/privacy` mentions the
  provider label; a `remember` tool call then `/memory` shows it; `/forgetme` asks for
  confirmation.
- The system prompt loads through `importlib.resources`.
- Heartbeat task touches the file (short interval injected).
- `sb-bot-admin es-setup` with a fake client: license check, templates called, key request has no
  cluster privileges and the right index privileges and expiry; password via patched `getpass`
  never appears in output.

Integration (marked, skip without services): build the app against the dev Postgres
(`TEST_PG_DSN`, default `postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test`), Valkey
(`redis://127.0.0.1:56379/15`, **never port 6379**) and, if reachable, ES at
`http://127.0.0.1:59200` with a random prefix (create templates via `ensure_templates`, then
clean up). Run one turn with `ScriptedProvider` and `FakeEmbedder` passed as the override.

Command: `uv run pytest apps -q` and then `uv run pytest -q`.

## Done when

- `uv run sb-bot` works on the console with no services, and with `PG_DSN` set to the dev
  Postgres (describe in the report how you checked; the sandbox may not reach Postgres, in which
  case say so).
- All tests above pass, the full suite passes, `ruff check .` is clean.
- `.env.example` covers every variable read anywhere in the app or deploy files.
