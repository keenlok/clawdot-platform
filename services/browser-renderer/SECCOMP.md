# Chromium sandbox profile

`seccomp.json` is Microsoft's Playwright v1.64.1 Docker profile (Apache-2.0):
https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json

Three additions are required on the target Ubuntu 24.04 host with runc 1.3.3 and all container
capabilities dropped:

- Allow `openat2`: modern runc uses it when securing its `/proc` handles after applying seccomp.
- Return ENOSYS (38) for `clone3`: glibc then falls back to the permitted `clone` syscall.
- Allow `chroot`: Chromium uses it inside its unprivileged user namespace. Its initial
  process still has no host capabilities; the conditional CAP_SYS_CHROOT rule is insufficient.

The original namespace allowances remain. Chromium's own sandbox, Docker's default AppArmor
profile, non-root execution and no-new-privileges all remain enabled. No host sysctl is changed.
