# Platform and private deployment split

Status: **Live** — split complete; clean public history selected (2026-11-03).

## Repository responsibilities

`clawdot-platform` contains `packages/`, framework `services/` code and a working
`apps/_scaffold/`. The scaffold includes a generic persona, minimal core plugins,
console example, compose/deploy skeleton, environment template and README checklist.
Platform CI tests its wiring, installs built wheels outside the checkout, and builds
and exercises its container.

The owner-only `clawdot-deployments` repository contains private applications under
`bots/`, per-bot encrypted `secrets/`, private `config/` and shared deployment,
autodeploy, backup and release-control configuration. Each bot has its own encrypted
secret file and recipients. Write access there is deploy access for every bot; it is
never an invitation target for outside contributors.

## Dependency and release boundary

Downstream applications pin reviewed platform Git releases, with a lockfile and their
own application CI. Platform changes are validated against the real scaffold; private
applications test explicit pin updates and scheduled compatibility with platform main.
A platform merge does not deploy a bot. A root version bump publishes only a Git tag
after both trusted-main CI jobs succeed, with immutable-tag checks.

The private app and its integration tests moved successfully. Deployment, legacy rollback,
forward deployment and the independent automatic path were verified before app/operator
paths were removed from platform. Exact host, account, backup and rollout evidence stays
in private records.

## History decision

The owner selected a fresh public history from the audited framework tree. The
[earlier repository](https://github.com/keenlok/clawdot-platform-history) remains private;
historical PR links target that archive. No old Git objects or parent commits are imported.
Infrastructure identifiers, real Telegram identities, travel records and application
persona remain private. Generic synthetic examples and the scaffold persona are retained.

The clean history starts with an audited snapshot at root version `0.0.0`, followed by a
`0.1.1` version commit. Individual runtime package versions are reviewed independently.

## Release control

Reusable release-control code remains here; manifests, operational configuration and
evidence remain private. Cross-repository release control stays inactive until a separately
reviewed design binds both the platform pin and deployment commit. Generic network aliases
replace deployment-specific names in its inactive example/policy validation without changing
network scope, capability rules or any other permission check. Fresh policy/evidence is
required before activation.

Architecture section numbers remain stable. Production-only sections link to private
originals; framework feature plans remain indexed. See the
[execution record](2026-11-03-backup-and-split-execution.md).
