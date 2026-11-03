# Self-extension worker implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. The root orchestrator owns dispatch/review; do not create additional agents or commit/push.

**Goal:** Build and verify a real, isolated local execution worker for bounded Python JSON transforms.

**Architecture:** A new extractable package contains immutable artifact validation, an asynchronous Docker executor, an authenticated Unix-socket HTTP service and its client. The service alone accesses Docker; generated code receives no host mounts, credentials or network. This stage is independently testable infrastructure, not completion of the chat feature; continue with the controller plan.

**Tech Stack:** Python 3.12, uv workspace, dataclasses, stdlib JSON/hashlib/HTTP/socketserver, httpx, pytest, local Linux Docker.

---

Read `plans/2026-10-31-self-extension-design.md`, `plans/COMMON.md`, `ARCHITECTURE.md` §6a and existing package metadata first. Current session authorization overrides COMMON's historical statement that Docker is unavailable: real local sandbox tests are required and authorized. Do not touch production, install runtimes, change Docker daemon policy, or run generated source in the host Python interpreter. Work only in this isolated checkout. Root reviews/commits; no implementation-agent commits.

## Files and stable interfaces

Create under `packages/plugin-extensions/`:

- `pyproject.toml`: project `chatcore-extensions`, dependency `chatcore`, `httpx>=0.27`, build backend matching other packages; CLI entry point `sb-extension-worker = chatcore_extensions.worker:main`.
- `src/chatcore_extensions/__init__.py`: package exports only.
- `models.py`: frozen artifact/test/result records, strict validation, canonical encoding and digest.
- `sandbox.py`: fixed `SandboxPolicy`, `DockerSandbox`, trusted harness constant and lifecycle cleanup.
- `worker.py`: Unix socket server, token-file loading, bounded routes, concurrency and CLI.
- `client.py`: `WorkerClient` using httpx Unix transport, typed failures and explicit close.
- `tests/test_models.py`, `test_sandbox.py`, `test_worker.py`, `test_sandbox_integration.py`.

Root workspace `pyproject.toml` adds only `chatcore-extensions = { workspace = true }` to `[tool.uv.sources]` (the `packages/*` member glob already includes the package). Update `uv.lock` by `uv sync --all-packages`. This narrowly necessary root mapping is an explicit exception to old track ownership guidance, not permission for unrelated dependency changes.

Contracts to implement (all new; no changes to core contracts):

```python
@dataclass(frozen=True)
class ExampleCase:
    arguments: dict[str, object]
    expected: object  # finite, JSON-serializable

@dataclass(frozen=True)
class Artifact:
    description: str
    source: str
    cases: tuple[ExampleCase, ...]
    # canonical versioned JSON and sha256 properties; no caller-supplied digest override

@dataclass(frozen=True)
class RunResult:
    status: str  # ok, invalid, timeout, resource_limit, output_limit, execution_error, unavailable
    value: object | None
    duration_ms: int
    image_id: str
    policy_digest: str
    harness_digest: str

class DockerSandbox:
    async def run(self, artifact: Artifact, arguments: dict[str, object]) -> RunResult: ...

class WorkerClient:
    async def health(self) -> dict[str, str]: ...
    async def run(self, *, chat: ChatRef, run_id: str,
                  artifact: Artifact, arguments: dict[str, object]) -> RunResult: ...
    async def aclose(self) -> None: ...
```

`Artifact` is no security proof. Do not attempt an AST allowlist or restricted Python builtins as an isolation substitute. Reject invalid JSON/non-finite floats and duplicate keys, unknown envelope fields, unsupported protocol versions, invalid UTF-8, source over 32 KiB, arguments/results over 16 KiB, more than eight cases, excessively nested structures (maximum depth 16), and descriptions over 1 KiB. Explicitly reject zero cases for activation-quality test artifacts. Canonical encoding uses `ensure_ascii=False`, `allow_nan=False`, `sort_keys=True`, compact separators and UTF-8; include `version: 1`. External request hashes must match recomputation.

## Task A1 — immutable bounded artifacts (SE-04, SE-06)

- [x] Add failing tests for stable digest under object-key reordering, changed source/case changing digest, unknown fields, duplicate keys, non-finite floats, depth and byte limits. Assert exact boundary values are accepted and boundary+1 rejected. Test `{"source":"...","chat_key":"other"}` is invalid as an artifact.
- [x] Run `uv run pytest packages/plugin-extensions/tests/test_models.py -q`; verify failure is the missing package/interface, not import-path configuration.
- [x] Implement the records, strict parse/serialize helpers, validation exception with fixed safe messages, and metadata. Source is data only: never `exec`, import, compile, subprocess it or evaluate type annotations on the host.
- [x] Add package/source metadata, synchronize lock, then rerun model tests and `uv run ruff check packages/plugin-extensions`.

## Task A2 — real Docker lifecycle (SE-05, SE-06)

- [x] Add argv-builder tests asserting exact immutable image ID, no mounts, no env propagation, no shell, no host PID/network, no privileges, fixed entrypoint and every limit. Fake process tests cover start failure, deadline, cancellation, output overflow and removal failure. Assert cleanup names only the generated `sb-ext-<random hex>` container and does not use prune/list-all/remove-by-label.
- [x] Implement subprocess execution with `asyncio.create_subprocess_exec`; use a configured Docker executable and a locally resolved `sha256:<64hex>` image ID. Worker CLI may accept a human image reference for **inspection only**, resolves it once, fails if missing, and never pulls/builds. Record image ID plus available RepoDigest in health output. Local known RepoDigest is `python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`; inspect the actual local image before using it.
- [x] Implement create/start/attach/inspect/remove using fixed flags: `--network none --user 65534:65534 --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --memory-swap 128m --cpus 0.5 --pids-limit 32 --tmpfs /tmp:rw,noexec,nosuid,size=16m --workdir /tmp --entrypoint python`. Use private default PID namespace, default seccomp, no extra devices, no Docker socket, no host mounts. Set no image entrypoint overrides from requests. Use `python -I -B -c <fixed harness>` and stdin for the source/input envelope. Confirm flags work against the actual local image and daemon; don't weaken them to make tests pass.
- [x] Harness accepts one bounded envelope, executes source in a fresh namespace, calls `run(arguments)`, and emits bounded JSON. Generated stdout/stderr are untrusted; capture them inside the container for protocol hygiene, but also enforce an outer combined stream byte limit because source can bypass Python redirection. Host parses one strict JSON value and rejects extra output. Never expose raw stderr to users or logs. A forged result is data, not authority or test evidence.
- [x] Enforce a five-second wall deadline across running/attach; inspect OOM state for resource failures, and return fixed status codes. A separate bounded cleanup deadline runs in `finally`, including cancellation. Create/start have bounded deadlines too. Track names created by this process for shutdown cleanup. Reap child processes. Use memory caps while reading streams; `communicate()` with an unbounded output buffer is not acceptable.
- [x] Run unit tests and manually inspect one created container's effective settings before cleanup as part of the integration fixture. Passing argv assertions alone is insufficient.

## Task A3 — authenticated worker/client (SE-06)

- [x] Write tests for absent/wrong token, absent/unsafe token file, wrong socket permissions, bad content length, chunked encoding rejection, overlarge body, unknown fields, unsupported method/path, malformed artifact digest, disconnect, and concurrency saturation. Authentication failure must occur before parsing artifact/executing Docker. Assert tokens/source/arguments do not appear in captured logs.
- [x] Implement local Unix HTTP server with `ThreadingMixIn + UnixStreamServer`, a bounded semaphore of two jobs and immediate `503` when busy. Each handler can run the async sandbox via a dedicated event loop; subprocess state must not share an event loop across threads. Accept only `/health` GET and `/run` POST with JSON and valid Content-Length; cap total envelope at 128 KiB and socket read timeout. Use constant-time bearer-token comparison. Reject untrusted image/policy/entrypoint/resource fields. Require a valid namespaced scope and opaque bounded run ID, but never expose a route for another job's data.
- [x] CLI flags: `--socket`, `--token-file`, `--image`. Require existing token file owned by the current UID and mode 0600; parent socket directory mode 0700, no symlink, reject preexisting non-socket paths. Create socket mode 0600 and remove only the owned socket on orderly exit. Require a sufficiently long random token. Do not create a TCP listener or load `.env`.
- [x] Implement client with explicit 15-second transport deadline (job is five seconds), no redirects/proxies, bounded response streaming, bearer token absent from repr/errors, no retries that duplicate jobs, and `aclose`. Use `httpx.AsyncHTTPTransport(uds=...)`; serialize trusted scope from required `ChatRef`. Convert transport/protocol failures to fixed safe exceptions; never fall back to host execution.
- [x] Test service over a real temporary Unix socket with a fake sandbox, then test real worker+Docker one successful transform and one timeout. Verify server shutdown and client closure.

## Task A4 — adversarial real sandbox evidence (SE-05)

- [x] Add `sandbox_integration` pytest marker in root config. Tests skip by default unless `SB_EXTENSION_SANDBOX_TESTS=1`; when explicitly enabled, unavailable Docker/image must **fail**, not skip. Do not use the existing `integration` marker because the baseline command deselects it and these tests have a different prerequisite.
- [x] Run real malicious artifacts: read a host temporary sentinel through absolute path and `../../`; create a symlink in `/tmp` to host sentinel/`/app` and try writing; check `/var/run/docker.sock`, `/run/secrets`, host home and repository are absent; inspect environment for a planted host-only secret; attempt IPv4/IPv6 outbound socket and DNS; write `/etc`; create `/tmp` data and prove next run cannot read it.
- [x] Run bounded adversarial cases for infinite loop, output flood using `os.write`, memory allocation beyond 128 MiB, repeated fork beyond 32 PIDs, and filling `/tmp` beyond 16 MiB. Assert timeout/resource/output denial within a bounded total time, no leftover containers and a successful clean job immediately after each failure. PID test must reap children and parent cleanup still runs. Never run probes on the host.
- [x] Include a mount/path injection attempt in worker fields and ensure it is rejected before create. Capture `docker inspect` settings as test assertions. Test cancellation by cancelling an actual running client/controller task and verify eventual worker deadline/cleanup; client disconnect is not an instant-termination guarantee.
- [x] Execute:

```sh
uv run pytest packages/plugin-extensions/tests -q -m 'not sandbox_integration'
SB_EXTENSION_SANDBOX_TESTS=1 uv run pytest packages/plugin-extensions/tests/test_sandbox_integration.py -q
uv run ruff check packages/plugin-extensions
uv run ruff format --check packages/plugin-extensions
```

Record counts, runtime/image/policy/harness identity and any unsupported enforcement as a blocker, not a successful skip. Root can repeat live verification as reviewer. Leave no containers/socket files running after tests. Continue directly to the controller plan once reviewed.
