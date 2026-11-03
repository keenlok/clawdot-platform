from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

MAX_SOURCE_BYTES = 32 * 1024
MAX_ARGUMENT_BYTES = 16 * 1024
MAX_DEPTH = 16
MAX_CASES = 8
MAX_DESCRIPTION_BYTES = 1024


class ArtifactValidationError(ValueError):
    """Raised when untrusted artifact data violates the fixed protocol."""


def _fail() -> ArtifactValidationError:
    return ArtifactValidationError("invalid extension artifact")


def _json_value(value: object, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise _fail()
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _fail()
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _fail()
            _json_value(item, depth + 1)
        return
    raise _fail()


def canonical_json(value: object) -> bytes:
    _json_value(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fail() from exc


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise _fail()


@dataclass(frozen=True, init=False)
class ExampleCase:
    _arguments_json: bytes
    _expected_json: bytes

    def __init__(self, arguments: dict[str, object], expected: object) -> None:
        if not isinstance(arguments, dict) or any(not isinstance(k, str) for k in arguments):
            raise _fail()
        argument_bytes = canonical_json(copy.deepcopy(arguments))
        expected_bytes = canonical_json(copy.deepcopy(expected))
        if len(argument_bytes) > MAX_ARGUMENT_BYTES or len(expected_bytes) > MAX_ARGUMENT_BYTES:
            raise _fail()
        object.__setattr__(self, "_arguments_json", argument_bytes)
        object.__setattr__(self, "_expected_json", expected_bytes)

    @property
    def arguments(self) -> dict[str, object]:
        return json.loads(self._arguments_json)

    @property
    def expected(self) -> object:
        return json.loads(self._expected_json)


@dataclass(frozen=True)
class Artifact:
    description: str
    source: str
    cases: tuple[ExampleCase, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.description, str) or not isinstance(self.source, str):
            raise _fail()
        try:
            description_bytes = self.description.encode("utf-8")
            source_bytes = self.source.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _fail() from exc
        if len(description_bytes) > MAX_DESCRIPTION_BYTES or len(source_bytes) > MAX_SOURCE_BYTES:
            raise _fail()
        if not isinstance(self.cases, tuple) or not 1 <= len(self.cases) <= MAX_CASES:
            raise _fail()
        if any(not isinstance(case, ExampleCase) for case in self.cases):
            raise _fail()

    @property
    def canonical(self) -> bytes:
        return canonical_json(
            {
                "version": 1,
                "description": self.description,
                "source": self.source,
                "cases": [
                    {"arguments": case.arguments, "expected": case.expected} for case in self.cases
                ],
            }
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    def to_json(self) -> str:
        return self.canonical.decode("utf-8")

    @classmethod
    def from_json(cls, raw: str | bytes) -> Artifact:
        try:
            raw_size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            if raw_size > 128 * 1024:
                raise _fail()
            text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
            value = json.loads(
                text,
                object_pairs_hook=_pairs_no_duplicates,
                parse_constant=_reject_constant,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise _fail() from exc
        if not isinstance(value, dict) or set(value) != {
            "version",
            "description",
            "source",
            "cases",
        }:
            raise _fail()
        if type(value["version"]) is not int or value["version"] != 1:
            raise _fail()
        if not isinstance(value["cases"], list):
            raise _fail()
        cases: list[ExampleCase] = []
        for item in value["cases"]:
            if not isinstance(item, dict) or set(item) != {"arguments", "expected"}:
                raise _fail()
            cases.append(ExampleCase(item["arguments"], item["expected"]))
        return cls(value["description"], value["source"], tuple(cases))


@dataclass(frozen=True)
class RunResult:
    status: str
    value: object | None
    duration_ms: int
    image_id: str
    policy_digest: str
    harness_digest: str

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in {
            "ok",
            "invalid",
            "timeout",
            "resource_limit",
            "output_limit",
            "execution_error",
            "unavailable",
        }:
            raise _fail()
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise _fail()
        if not isinstance(self.image_id, str) or not re_full_digest(self.image_id):
            raise _fail()
        if not isinstance(self.policy_digest, str) or not re_hex_digest(self.policy_digest):
            raise _fail()
        if not isinstance(self.harness_digest, str) or not re_hex_digest(self.harness_digest):
            raise _fail()
        if self.value is not None:
            _json_value(self.value)
            if len(canonical_json(self.value)) > MAX_ARGUMENT_BYTES:
                raise _fail()


def re_hex_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def re_full_digest(value: str) -> bool:
    return value.startswith("sha256:") and re_hex_digest(value[7:])
