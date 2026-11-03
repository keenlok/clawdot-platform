# Owner-only GitHub collaborator invite command

Status: **Record — unimplemented private-repository design** (2026-11-03). The split is
complete and the platform has a clean public history. Public contributions use forks and
PRs without an invite. Deployments stay owner-only and are never an invitation target.
The design below is retained as a historical security/interaction reference.
Replaces option (b) in the `ARCHITECTURE.md` §12 backlog entry "Share a contribute or learn
link in allowed groups"; option (a), a fixed `CONTRIBUTE_URL`, stays a separate, smaller item.
Related: `docs/plans/2026-11-01-human-pr-intake.md` (D1, D6), `docs/plans/2026-11-02-repo-split.md` and
`docs/plans/2026-11-02-secrets-sops-age.md`.

## Goal

The owner can invite a person to a GitHub repository from chat, with a confirmation that
shows the real GitHub account. The person accepts through GitHub; the bot never sees their
credentials.

## Hard prerequisite: an invite must not be deploy access

Before the split, an invite to the private application monorepo was effectively production access:

- The earlier application repository was a personal repo on GitHub Free. Collaborators have a single
  fixed role, **write**, and branch protection and rulesets are not available for private repos
  on Free (`human-pr-intake.md` §4.1).
- The old pull-based auto-deployer deployed the tip of `main` once CI passed
  (`docs/plans/2026-11-01-deploy-pipeline.md`, `docs/plans/2026-11-01-autodeploy-actions-gate.md`).
  Release control, which would fail closed on an unexpected `main` (§4.2 of the intake plan),
  is shipped but inactive.
- So an invitee can push to `main`, CI passes, and their code runs in the bot container with
  every secret in `.env`.

For any future private-repository invite design, **no real invite is sent** until one of these holds for the target repository:

| Option | Effect | Source |
| --- | --- | --- |
| D1(a) GitHub Pro + branch protection or a ruleset requiring PRs on `main` | Collaborators still have write but cannot push to `main` | intake plan §4.3 |
| D1(b) Move the repo to a free organization | Invitees get the **read** role and contribute through fork PRs | intake plan §4.3 |
| The target repo is not the one that deploys | After the split, the platform repo only publishes tags | `docs/plans/2026-11-02-repo-split.md` |

D6 (any write collaborator can read the CI gateway token from Actions secrets) also applies
under D1(a) and must be fixed (the intake plan's T6, CI→gateway auth via OIDC) or explicitly
accepted before the first write invite. Under D1(b) invitees have no write and D6 does not
apply.

## Which repository is the target

- **Before the split:** `keenlok/clawdot-platform`, only after D1 is resolved.
- **After the split:** the **platform** repo, and only if it stays private. If it is public,
  invites are unnecessary: anyone can fork and open a PR. The private **deployment** repo is
  never an invite target for outside contributors: write there is deploy access, and read there
  exposes the infrastructure notes and the encrypted secrets.
- `GITHUB_INVITE_REPO` names the target; the plugin refuses a repository that is listed in a
  new setting `GITHUB_INVITE_FORBIDDEN_REPOS` (set to the deployment repo).

## Behaviour

- Command `/invite <github-username-or-profile-url>`, declared as
  `Command(..., admin_only=True)` so non-admins get no reply (`chatcore.plugin.Command`, §6a).
  The handler additionally checks that the caller's user ID is in `GITHUB_INVITE_OWNERS`
  (default empty = feature off). In practice this list holds only the owner; it is a list so the
  ID is configuration, not code.
- Input is normalised: `https://github.com/octocat`, `@octocat` and `octocat` all become
  `octocat`. Anything that is not a valid GitHub login (`^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$`)
  is rejected before any request.
- **Lookup first.** `GET /users/{login}`. If it 404s the bot says so and stops. Otherwise the
  confirmation shows the canonical login, display name, account type (user or organization),
  account creation date and profile link, then asks "Invite as a collaborator?". An
  organization account is refused.
- **Confirm.** `await ctx.confirm.request(action, ctx.user)` with a pending action handled by
  the plugin's `action_handlers()`, like other writing commands. The confirmation expires and
  is bound to the requesting user and chat.
- **Invite.** `PUT /repos/{owner}/{repo}/collaborators/{login}`. On a personal repo GitHub
  ignores `permission` and grants write; on an organization repo the plugin sends
  `permission` from `GITHUB_INVITE_PERMISSION` (default `pull`, i.e. read). On 201 the bot
  replies with the invitation page (`https://github.com/{owner}/{repo}/invitations`), that
  GitHub emails the invitee, and that the invite expires after 7 days. On 204 (already a
  collaborator) it says so. On 403/404/422 it reports a credential-safe error.
- **Revoke.** `/uninvite <login>` removes a collaborator or deletes the pending invitation
  (`GET /repos/.../invitations` then `DELETE /repos/.../invitations/{id}`), with the same
  confirmation.
- The bot never DMs the invitee: a Telegram bot can only message users who started it, and
  the mapping from a Telegram user to a GitHub login is not known to the bot. The owner passes
  the reply on, or the invitee reads the GitHub email. Best practice: the invitee sends the
  owner their own profile link, which is pasted into `/invite`.

## Credential

- A credential separate from release control's GitHub App. `github_app.py` hard-checks that
  the release-control App has exactly `contents: write`, `pull_requests: write`,
  `metadata: read`; it is not widened.
- Preferred: a second GitHub App installed only on the target repository with
  **Administration: Read and write** and **Metadata: Read**, token minted per request (about an
  hour of lifetime). Fallback: a fine-grained personal access token scoped to that one
  repository with the same permissions.
- The App's private key (or the PAT) is a **new secret on the box**. It goes into the
  secrets inventory and the encrypted secrets of `docs/plans/2026-11-02-secrets-sops-age.md`,
  never into code, tests or logs. New settings: `GITHUB_INVITE_REPO` (`owner/name`),
  `GITHUB_INVITE_OWNERS`, `GITHUB_INVITE_FORBIDDEN_REPOS`, `GITHUB_INVITE_PERMISSION` and the
  credential settings for the chosen mechanism.
- An administration-scoped credential on the bot box is a high-value target if the bot is
  prompt-injected. Mitigations: slash commands only (no LLM tool, so a model cannot be talked
  into it, as with `/model`); `admin_only` plus the owner list; confirmation; the forbidden-repo
  check; and the audit log below.

## Audit and limits

- Each invite and revoke logs the acting user ID, chat, target login, repository, result and
  GitHub status code, never the token.
- Rate limit: at most 5 invites per owner per hour.

## Where it lives

A new plugin `packages/plugins/github_admin/` (`chatcore_github_admin`), following the module
layout in `AGENTS.md`: `plugin.py` (commands and `action_handlers()`), `settings.py`,
`service.py` (the GitHub client) and tests with a faked GitHub API.

- **HTTP client:** core's `safe_get` (`chatcore.security.net_guard`) only does GET, and the
  invite needs PUT and DELETE. The service uses its own small `httpx` client pinned to
  `https://api.github.com` (fixed host, no user-controlled URL, so the SSRF guard is not
  needed), with timeouts and no redirects.
- **Gates:** `ToolGate` applies to LLM tools only (`chatcore.skills.ToolGate`,
  `Plugin.tool_gates()`); commands are restricted with `Command.admin_only` and the handler
  check. The plugin has no tools, so it has no `gates.py` and no skill. The PR still calls out
  the owner check as a permission change.
- No skill, routing-set or request-snapshot change; the snapshot must pass unchanged. Off until
  `GITHUB_INVITE_OWNERS` and the credential are set.

After the repo split the plugin stays in the platform repo as generic code; the target
repository, owner list and credential live in the deployment configuration.

## Ordering

1. Owner decisions below, D1 first.
2. If D1(a) or write collaborators: fix or accept D6.
3. Create the credential, add it to the encrypted secrets, set the settings.
4. Dry run: invite a second account the owner controls, accept, check it cannot push to `main`
   (D1(a)) or has read only (D1(b)), then `/uninvite` it.

## Tests

- Input normalisation and rejection (URLs, `@`, bad characters, over-long names).
- Lookup 404 stops before any write; organization accounts are refused; the confirmation text
  includes the canonical login and creation date.
- Non-admins get no reply; admins not in `GITHUB_INVITE_OWNERS` get a refusal and no side
  effects; the owner check runs again in the action handler at confirmation time.
- A forbidden target repository is refused at startup and at call time.
- 201, 204, 403, 404, 422 and network error responses from a fake GitHub.
- The token never appears in logs or error replies.
- Confirmation expiry and binding to user and chat.

## Decisions for the owner

| Id | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| I0 | D1: how invitees are kept from deploying | GitHub Pro + protection / free organization / only invite to a non-deploying repo | Free organization (read role, fork PRs), or wait for the split |
| I1 | Credential type | Second GitHub App / fine-grained PAT | Second GitHub App |
| I2 | Who may invite | Owner only / any bot admin | Owner only |
| I3 | Add `/uninvite` now | Yes / later | Yes |
| I4 | D6 (CI token exposure) | OIDC first / accept the risk | OIDC first, if invitees have write |
| I5 | Build before or after the repo split | Before (needs D1) / after | After, targeting the platform repo, unless it goes public (then not needed) |

## Execution and review

Planned in a cloud Claude session without Codex; to be implemented in the same way and
self-reviewed, per `AGENTS.md` "Execution and review". The owner may add a Codex review from
their computer.
