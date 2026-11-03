"""Publish a version tag for a reviewed main commit without deploying an application."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

_GIT = shutil.which("git")
if _GIT is None:
    raise RuntimeError("Git is required for platform releases")

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class ReleaseError(ValueError):
    """A release would violate version or immutable-tag rules."""


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        [_GIT, "-C", str(repository), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _version(repository: Path, commit: str) -> tuple[int, int, int]:
    document = tomllib.loads(_git(repository, "show", f"{commit}:pyproject.toml"))
    value = document["project"]["version"]
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ReleaseError("Root project version must be a plain major.minor.patch version")
    return tuple(map(int, value.split(".")))


def release_tag(repository: Path, commit: str) -> str | None:
    """Return a new tag, or None for unchanged versions/already published exact retries."""
    target = _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}")
    # Compare the reviewed merge to main before the merge, never to its PR tip.
    previous = _git(repository, "rev-parse", "--verify", f"{target}^1")
    before, after = _version(repository, previous), _version(repository, target)
    if after == before:
        return None
    if after < before:
        raise ReleaseError("Root project version must increase for a release")
    tag = "v" + ".".join(map(str, after))
    existing = _git(repository, "tag", "--list", tag)
    if existing:
        if _git(repository, "rev-parse", f"refs/tags/{tag}^{{commit}}") != target:
            raise ReleaseError(
                f"Existing {tag} points to a different commit; refusing to replace it"
            )
        return None
    published = [
        tuple(map(int, name[1:].split(".")))
        for name in _git(repository, "tag", "--list", "v*").splitlines()
        if _VERSION.fullmatch(name[1:])
    ]
    if published and after <= max(published):
        raise ReleaseError("Root version must exceed all published platform release tags")
    return tag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--publish", action="store_true")
    arguments = parser.parse_args()
    repository = Path.cwd()
    tag = release_tag(repository, arguments.commit)
    if tag is None:
        print("No new platform tag: version unchanged or exact tag already published")
        return
    if not arguments.publish:
        print(f"Would publish {tag}")
        return
    _git(repository, "tag", tag, arguments.commit)
    # No force flag: a concurrent or different remote tag also fails closed.
    _git(repository, "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    print(f"Published platform tag {tag}")


if __name__ == "__main__":
    main()
