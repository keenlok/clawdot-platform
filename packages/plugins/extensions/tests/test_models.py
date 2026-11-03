import json

import pytest
from chatcore_extensions.models import Artifact, ArtifactValidationError, ExampleCase


def test_canonical_digest_ignores_object_key_order() -> None:
    left = Artifact(
        "convert", "def run(arguments): return arguments", (ExampleCase({"x": 1, "y": 2}, 3),)
    )
    right = Artifact(
        "convert", "def run(arguments): return arguments", (ExampleCase({"y": 2, "x": 1}, 3),)
    )

    assert left.digest == right.digest


def test_source_or_case_change_changes_digest() -> None:
    baseline = Artifact("a", "x", (ExampleCase({}, 1),))
    assert Artifact("a", "y", (ExampleCase({}, 1),)).digest != baseline.digest
    assert Artifact("a", "x", (ExampleCase({}, 2),)).digest != baseline.digest


def test_parser_rejects_unknown_fields_and_duplicate_keys() -> None:
    with pytest.raises(ArtifactValidationError):
        Artifact.from_json(
            '{"version":1,"description":"x","source":"x","cases":[{"arguments":{},"expected":1}],"chat_key":"other"}'
        )
    with pytest.raises(ArtifactValidationError):
        Artifact.from_json(
            '{"version":1,"description":"x","source":"x","source":"y","cases":[{"arguments":{},"expected":1}]}'
        )


def test_parser_rejects_nonfinite_depth_and_bad_case_boundary() -> None:
    with pytest.raises(ArtifactValidationError):
        Artifact.from_json(
            '{"version":1,"description":"x","source":"x","cases":[{"arguments":{},"expected":NaN}]}'
        )
    deep_value = 0
    for _ in range(17):
        deep_value = [deep_value]
    nested = json.dumps(
        {
            "version": 1,
            "description": "x",
            "source": "x",
            "cases": [{"arguments": {"a": deep_value}, "expected": 1}],
        }
    )
    with pytest.raises(ArtifactValidationError):
        Artifact.from_json(nested)
    with pytest.raises(ArtifactValidationError):
        Artifact("x", "x", ())


def test_boundary_values_are_accepted_and_boundary_plus_one_rejected() -> None:
    Artifact("d" * 1024, "s" * 32768, (ExampleCase({}, 1),))
    with pytest.raises(ArtifactValidationError):
        Artifact("d" * 1025, "s", (ExampleCase({}, 1),))
    with pytest.raises(ArtifactValidationError):
        Artifact("d", "s" * 32769, (ExampleCase({}, 1),))


def test_artifact_is_json_data_not_a_chat_scope() -> None:
    with pytest.raises(ArtifactValidationError):
        Artifact.from_json(
            '{"version":1,"description":"x","source":"x","chat_key":"other","cases":[{"arguments":{},"expected":1}]}'
        )


def test_case_snapshots_mutable_inputs_and_returns_independent_values() -> None:
    arguments = {"nested": [1]}
    case = ExampleCase(arguments, {"answer": [2]})
    artifact = Artifact("x", "x", (case,))
    digest = artifact.digest

    arguments["nested"].append(3)
    observed = case.arguments
    observed["nested"].append(4)
    assert artifact.digest == digest
    assert case.arguments == {"nested": [1]}


def test_run_result_validates_remote_metadata_and_strict_integer_types() -> None:
    from chatcore_extensions.models import RunResult

    valid = {
        "status": "ok",
        "value": False,
        "duration_ms": 1,
        "image_id": "sha256:" + "a" * 64,
        "policy_digest": "b" * 64,
        "harness_digest": "c" * 64,
    }
    assert RunResult(**valid).value is False
    for field, value in (
        ("duration_ms", True),
        ("duration_ms", 1.0),
        ("image_id", "python:latest"),
        ("policy_digest", "b" * 63),
        ("harness_digest", "not-a-digest"),
        ("value", "x" * 16385),
    ):
        with pytest.raises(ArtifactValidationError):
            RunResult(**{**valid, field: value})
