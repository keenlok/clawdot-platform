# Scoped local extension worker

`chatcore-extensions` provides an opt-in workflow for pure JSON transformations in local or test
chats. A user proposes a transformation, confirms the request, and a current chat administrator
confirms a bounded build. The controller asks the configured Provider for one candidate, then tests
the request-confirmed examples in a separate worker and ephemeral Docker container. In v1, generated
examples returned with the candidate are discarded: the test evidence covers only the exact examples
shown to and confirmed by the requester. Those examples arrive through the conversational assistant,
so confirmation records what the requester approved but does not independently establish semantic
correctness. An administrator reviews the complete artifact manifest over bounded pages, including
its description, cases, source, digest, and sandbox identity, then confirms activation against that
identity. Members can invoke the approved revision with JSON arguments; status shows per-scope
build/test state, invocation counts, and daily quota use without exposing source, arguments, or
results. Draft status is visible only to its requester and a current administrator. Proposal
confirmation is limited to 3,000 canonical JSON bytes (and at most 3,800 displayed UTF-8 bytes) so
the complete request fits in the confirmation.

The generated source runs only in the worker container. The container has no network, host mounts,
credentials, package installation, or external-write capability. Local development defaults to the
explicit `runc` runtime. A production trial requires the separate root-owned worker service and
verified `runsc` package pinned by the downstream deployment runbook; the bot receives only a
read-only bind of the worker state directory and never receives Docker access. The worker is trusted
host infrastructure because Docker-group access is effectively host-root authority. The `runsc`
guest kernel and its package digest are bound into worker health, policy evidence, and approval.
Changing the runtime package changes the policy identity, so existing evidence must be regenerated
and reviewed before activation. gVisor reduces exposure but is not a proof against kernel/runtime
vulnerabilities. Keep the feature disabled unless an operator has completed the documented trial
preflight and acceptance. Generated artifacts have content digests; this version does not export
generated source as Git commits and does not modify the bot framework.

## Local smoke flow

Use a disposable local/test bot, test group, provider credential, and development database. Do not
copy production credentials or connect this worker to production. The worker needs Docker access;
the bot does not receive the Docker socket.

1. Create a private worker directory and random token file:

   ```sh
   umask 077
   worker_dir="$(mktemp -d /tmp/sb-ext-worker.XXXXXX)"
   chmod 700 "$worker_dir"
   python -c 'import secrets; print(secrets.token_urlsafe(48))' > "$worker_dir/token"
   chmod 600 "$worker_dir/token"
   ```

2. Confirm the local image and its immutable ID. This command does not pull or build an image:

   ```sh
   docker image inspect python:3.12-slim --format '{{.Id}} {{join .RepoDigests ","}}'
   ```

   The worker resolves the image reference once and starts only the resulting full `sha256:` image
   ID. The current planning environment used the `colima` Docker context and image ID
   `sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`; your local image may
   have a different ID.

3. Launch the worker from a clean environment. Add `DOCKER_CONTEXT` and, only when your local Docker
   context requires it, the specific `DOCKER_CONFIG` path. Keep bot tokens, provider keys, database
   credentials, Telegram credentials, and deployment variables out of this process.

   ```sh
   env -i PATH="$PATH" HOME="$HOME" DOCKER_CONTEXT=colima \
     uv run --package chatcore-extensions sb-extension-worker \
     --socket "$worker_dir/worker.sock" \
     --token-file "$worker_dir/token" \
     --image python:3.12-slim
   ```

4. In a separate terminal, configure only your test controller. Set `EXTENSIONS_ENABLED=true`,
   `EXTENSIONS_WORKER_SOCKET` to the private socket, and `EXTENSIONS_WORKER_TOKEN_FILE` to the token
   file. Enable the existing test-chat allowlist, development Postgres, and a test Provider key.
   Do not pass the token contents through an environment variable. Startup checks the authenticated
   worker's image, policy, and harness identities. Feature configuration is disabled by default.

5. In the test group, ask the bot to create a deterministic pure transform, such as converting
   Celsius to Fahrenheit, and provide at least one concrete input/output example. Confirm the
   proposal as the requester. A current group administrator runs
   `/extensions@<bot_username> build <request-id>` and confirms the exact build. Review with
   `/extensions@<bot_username> review <revision-id>`, inspect the full digest, cases and paginated
   source, then activate with
   `/extensions@<bot_username> activate <revision-id> <full-sha256-digest>` and confirm. A regular
   member can then ask the bot to use the approved extension. Monitor with
   `/extensions@<bot_username> status [revision-id]`; revoke immediately with
   `/extensions@<bot_username> revoke <revision-id>`.

   In a private DM, the chat owner can use `/extensions build`, `/extensions review`,
   `/extensions activate`, `/extensions status`, and `/extensions revoke`. DM artifacts remain in
   the owner's exact DM scope; they are not copied into any group.

6. Stop the worker with Ctrl-C or SIGTERM. It removes only its owned Unix socket and attempts cleanup
   for its exact tracked container names. Remove the private token file and directory after the
   worker and controller have stopped:

   ```sh
   rm -f "$worker_dir/token" "$worker_dir/worker.sock"
   rmdir "$worker_dir"
   ```

## Verification

```sh
uv run pytest packages/plugins/extensions/tests -q -m 'not integration and not sandbox_integration'
SB_EXTENSION_SANDBOX_TESTS=1 uv run pytest packages/plugins/extensions/tests/test_sandbox_integration.py -q
SB_EXTENSION_SANDBOX_TESTS=1 uv run pytest packages/plugins/extensions/tests/test_e2e.py -q
TEST_PG_DSN=postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test \
  uv run pytest packages/plugins/extensions/tests/test_pg_repository.py -q -m integration
```

The Docker tests require a preloaded local image and fail when explicitly enabled if Docker or the
image is unavailable. Postgres tests use a unique temporary schema in the documented development
database and remove it after the test. Ordinary container isolation checks are empirical tests, not
a security proof against kernel/runtime exploitation.
