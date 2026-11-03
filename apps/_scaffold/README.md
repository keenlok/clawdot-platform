# Working bot scaffold

This is a small application built on the platform. It runs locally without credentials,
Postgres, Valkey or Elasticsearch, using the console adapter and in-memory stores. Messages
addressed to the bot receive an echo by default. The two core plugins provide time and
conversation commands. State disappears when the process exits.

From the platform repository root:

```sh
uv sync --locked --package example-bot
uv run --package example-bot example-bot
```

Try `@examplebot hello`, `/help@examplebot`, `/time@examplebot`, then `::quit`. Use `::as name`,
`::chat group`, and `::dm` to exercise console users, groups and DMs. Only the configured
allowlist is served; the example defaults to `console:group` and `console:me` for local use.

To use the generic packaged persona with an LLM, supply `LLM_PROVIDER`, `LLM_MODEL` and
`LLM_API_KEY` privately, plus `LLM_BASE_URL` for an OpenAI-compatible endpoint when needed.
The core constructs the provider; a blank `LLM_PROVIDER` keeps echo mode. The app loads
`example_bot.prompts/system.md` through package resources, so installation does not depend
on the repository directory. Never commit a filled environment file.

## Container and local deployment skeleton

```sh
cp apps/_scaffold/.env.example apps/_scaffold/.env
apps/_scaffold/deploy/deploy.sh build
apps/_scaffold/deploy/deploy.sh run
```

Compose reads the app-local `.env`; Python itself reads process environment only. The container
runs as a non-root user with a read-only filesystem, temporary scratch storage and no published
ports. This skeleton runs the interactive console, so it has no background restart policy or
server health endpoint. Add an adapter lifecycle and heartbeat when creating a hosted bot.

The Dockerfile currently builds from the platform workspace root with the checked-in lockfile.
It copies the workspace only into its build stage, installs the selected app non-editably,
and places only the resulting virtual environment in the runtime image. The existing SBot
build is unchanged by this example.

## Copy into a private deployment repository

Use `bots/<new_bot>/` as the destination and complete this checklist before deploying:

- Rename the `example-bot` distribution, `example_bot` Python package and CLI together; update
  the Dockerfile's selected package/command, Compose service and deploy-script service name.
- Customize the packaged persona and choose plugins through their package-level exports.
- Pin platform packages to reviewed releases in the private root configuration and lockfile.
  Do not copy the platform's workspace source mappings into the deployment repository.
- Update the Dockerfile's source inputs: replace the local `packages/` and `services/` copies
  with the deployment's pinned platform dependencies or verified built wheels, and copy
  `bots/` instead of `apps/`. Update the Compose Dockerfile path to `bots/<new_bot>/Dockerfile`.
  The build context remains the deployment repository root, two levels above the bot.
- Set explicit adapter/chat/admin configuration. Add durable stores if the bot needs them;
  in-memory example state is unsuitable for durable production data.
- Give the bot its own private config, encrypted secret file and host age recipient. Use the
  shared deployment pipeline, backups and runtime hardening; never share another bot's secrets.
- Run the app's tests against its pinned platform artifacts, build its image, verify health,
  and perform a rollback before enabling automatic deployment.

## What platform CI checks

CI runs the real console and scripted agent tests, builds core/console/example wheels, installs
those wheels into a fresh virtual environment and runs `tests/installed_smoke.py` from outside
the checkout with `PYTHONPATH` unset. That smoke checks installed import locations, the packaged
persona, agent wiring and the console entry point. CI also builds and exercises the actual
container. A framework change breaking application wiring therefore fails platform CI.
