# Clawdot Platform

Clawdot is a modular Python chat bot framework with console and Telegram adapters,
capability plugins, and standalone browser-rendering and release-control services.
`apps/_scaffold` is a working generic app: platform CI tests its framework wiring,
installs its wheels outside the checkout, and builds and runs its container.

Private bot personas, plugin selections, composites, configuration, encrypted secrets
and operator scripts belong in the owner-only `clawdot-deployments` repository.
Downstream applications pin reviewed platform releases and run their own CI/deployment.
A platform merge validates the framework and scaffold.

## Capabilities

- Scoped conversations, administration, provider integrations and access controls.
- Search, web fetching, rendered browsing, memory, stocks and research.
- Expenses, receipt processing, CSV export, polls, reminders and website watches.
- Reviewed sandboxed extensions for pure JSON transformations, with confirmation,
  testing, activation, monitoring and revocation.

Availability depends on each application's plugin selection, configuration and backing
services. Release-control code is shipped but remains inactive pending a separately
reviewed design for releases across repositories.

## Repository layout

| Path | Purpose |
| --- | --- |
| `packages/core/` | Framework, providers, storage, security and administration |
| `packages/adapters/*/` | Console and Telegram adapters |
| `packages/plugins/*/` | Capability plugins loaded by applications |
| `services/` | Browser renderer and protected release-control service code |
| `apps/_scaffold/` | Generic persona, minimal plugins, container and deploy skeleton |
| `tests/` | Package boundaries, service image guarantees and plan index checks |
| `docs/` | Reusable feature and integration guides |
| `ARCHITECTURE.md` | Architecture specification and decision record |
| `docs/plans/` | [Indexed plans and historical design records](docs/plans/README.md) |
| `docs/handovers/` | Framework handover records |

## Run the example

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Docker is needed
for optional development services and container tests.

```sh
uv sync --locked --all-packages
uv run --package example-bot example-bot
```

The example uses in-memory stores, a console adapter, core conversation/time commands
and local echo replies by default. It needs no credentials or backing services.
See the [scaffold README](apps/_scaffold/README.md) and
[environment template](apps/_scaffold/.env.example) for optional provider configuration,
container commands and the checklist for copying it into a downstream deployment.

## Development checks

```sh
uv run pre-commit install
uv run ruff check packages services apps tests .github/scripts
uv run ruff format --check .
uv run pytest -q -m 'not browser and not sandbox_integration'
```

Integration tests require disposable local services. Start them with
`docker compose -f compose.dev.yaml up -d` and set `TEST_PG_DSN` as documented in
[compose.dev.yaml](compose.dev.yaml). Search tests additionally need its search profile.
Use development credentials and temporary data only.

## Configuration and documentation

Keep credentials, production data, `.ini` files and private `.env` files outside Git.
Tracked environment templates contain configuration names and non-secret defaults.
Git ignore rules do not prevent force-adding secrets; pre-commit includes Gitleaks.
Deployments own encrypted backups, restore drills and runtime secret delivery.

- [Administration setup](docs/admin-setup.md)
- [Sandboxed extensions](packages/plugins/extensions/README.md)
- [Reminders and website watches](docs/reusable-jobs.md)
- [Rendered browsing](docs/rendered-fetch.md)
- [Research](docs/research.md)
- [Polls](docs/polls.md)

Historical plans may name the original app and pre-split paths. Production operator
records are preserved privately; stable architecture sections link to their archive.
This repository begins with a sanitized framework snapshot and fresh Git history.
Earlier development and application records are kept in a
[private history archive](https://github.com/keenlok/clawdot-platform-history).

## Platform release tags

The root `pyproject.toml` version is the release version. After both platform and scaffold
CI succeed for a trusted `main` push, a version increase relative to the merge's first
parent publishes its `v<version>` Git tag. Unchanged versions create no tag. Existing tags
are immutable: an existing tag on a different commit fails the release job. This job has
no deployment trigger; downstream apps explicitly update their pinned platform release.

The initial snapshot uses root version `0.0.0`, followed by the first clean-history
release at `0.1.1`. Earlier release pins remain in the private archive. Individual package
version changes are reviewed separately. This repository does not publish packages to a
registry.

Inactive release control uses generic `app-backend`/`app-internal` network aliases
and an `app-models` volume alias.
Its network allowlist and capability rules are unchanged; activation across repositories
requires a separately reviewed policy and fresh deployment evidence.
