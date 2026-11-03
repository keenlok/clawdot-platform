# P07: OIDC verifier and broker protocol

Current source status supersedes the historical increments below: the concrete
private broker service, renewable distinct Broker App, exact dispatch UUID/run/job
correlation, installed Git closure, phase-specific merge/switch admissions, owner
revocation and retained terminal receipts are implemented. Repository OIDC subject
format is explicitly configured. Ordinary and explicit reconciliation eligibility
are enforced against fresh Git facts both by the broker and hosted merge entrypoint;
card issuance checks and labels the same immutable review mode. Independent final
review passed 80 focused cases spanning this gate and source credential isolation.
The final combined regression passed 1001 tests (1 skipped, 10 deselected); real OIDC/network/provisioning and owner
production acceptance remain pending. See the full parts plan for current status.

First increment: implement real RS256 JWT signature verification against an injected
trusted JWKS snapshot using PyJWT; strict issuer/audience/time/repository/owner/workflow/
run/attempt/check-run/runner matching; require independent workflow-closure and job
correlation callbacks to succeed. No default acceptance callbacks and no external
requests in tests. Tokens and raw exceptions must never appear in safe errors/results.

Files: `src/chatcore_release_control/oidc.py`, `tests/test_oidc.py`, package metadata/uv.lock.
Tests generate ephemeral RSA keys, exercise correct signature and wrong signing key,
algorithm confusion, missing/expired/future claims, wrong audience/issuer/repo/job,
duplicate/unknown JWKS kid, callback outage, and closure/correlation failures. Run
focused pytest and Ruff. This increment does not install endpoints, fetch/caches JWKS,
persist receipts or claim a release; those remain P07 integration work after P05.

Reference contracts: GitHub OIDC reference and PyJWT usage documentation, consulted
2026-10-31. In particular `job_workflow_ref` is reusable-workflow-specific and is not
required for every job. `workflow_sha` is a full commit ID, not a content digest.

Local increment implemented in `oidc.py` with 16 focused tests passing and Ruff clean.
`installed_workflow_verifier` joins real RSA-signed token validation to independently
read Git blobs at the signed workflow commit; an integration fixture rejects a
mismatched installed workflow/helper digest despite a valid token signature.
The map is copied at construction and must be supplied by installed protected policy.
Job/run correlation remains a mandatory external protected callback. JWKS fetching,
cache expiry/refresh, HTTPS endpoints and ledger-backed receipts remain incomplete;
this verifier alone grants no release authority. Self-review: PASS for this increment,
P07 as a whole remains partial.

Further increment: `fetch_github_jwks()` now fetches the fixed issuer's published
`/.well-known/jwks` endpoint with a fresh no-credential, no-proxy, no-redirect HTTPS
client; response bytes/time and key count are bounded. Duplicate JSON keys, redirects,
invalid/oversized bodies and outages deny access. Synthetic HTTP transport tests pass;
no actual token verification request against GitHub was made. Focused P07 result is
17 passed. No JWKS cache is used, so stale-cache acceptance and cache-key rotation are
not assumptions; ingress quotas/retry/backoff remain service integration work.
References: https://docs.github.com/en/actions/reference/security/oidc and
https://token.actions.githubusercontent.com/.well-known/openid-configuration.

Independent review repair P07-R1: HTTPX inactivity timeouts did not establish a total
stream deadline. A valid delayed JWKS stream reproduced the gap. The entire fetch and
parse now runs under a five-second asyncio deadline, in addition to HTTPX I/O timeouts;
the regression passes. P07 focused count is now 18. Targeted independent re-review: PASS.
