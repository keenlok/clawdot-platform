# Chromium sandbox profile

`seccomp.json` is copied from the pinned Microsoft Playwright v1.64.1 Docker profile
(Apache-2.0). The production worker uses `pwuser` (UID/GID 1001), drops all Linux capabilities,
sets `no-new-privileges`, and keeps Chromium's own sandbox enabled. Use this profile through the
deployment's `security_opt`; do not add `--no-sandbox`, privileged mode or extra capabilities.

The profile keeps the renderer's narrow additions for `openat2`, `clone3` (ENOSYS fallback),
and `chroot` required by Chromium's unprivileged user-namespace sandbox. It does not change host
sysctls. The dedicated egress bridge must separately deny UDP, private destinations and every
public CONNECT host outside the worker's checked-in exact resource-host list.
