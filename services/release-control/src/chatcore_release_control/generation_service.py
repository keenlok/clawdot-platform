"""Explicit protected generation→sandbox→publication CLI with installed choices only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from chatcore.llm.factory import LLMSettings, build_provider
from pydantic import SecretStr

from .bridge_service import _secure_read
from .broker_http import ProtectedHostClient
from .development import (
    DevelopmentController,
    DockerSandboxRunner,
    GenerationRequest,
    ResourcePolicy,
    SandboxConfig,
)
from .development_worker import WorkerManifest
from .git_evidence import collect_release_bundle
from .publication import ScanInput, ScanVerdict
from .publication_service import ProtectedPublicationConfig, build_publication_gateway


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def parse_request(raw: bytes) -> dict:
    try:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= 32 * 1024:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_unique)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "request_id",
                "specification",
                "synthetic_examples",
                "title",
                "body",
            }
            or not isinstance(value["request_id"], str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value["request_id"]) is None
        ):
            raise ValueError
        for key in ("specification", "title", "body"):
            if (
                not isinstance(value[key], str)
                or "\0" in value[key]
                or len(value[key].encode()) > 8192
            ):
                raise ValueError
        if not value["specification"].strip() or not value["title"].strip():
            raise ValueError
        if (
            not isinstance(value["synthetic_examples"], list)
            or not 1 <= len(value["synthetic_examples"]) <= 8
            or len(json.dumps(value["synthetic_examples"], allow_nan=False).encode()) > 16 * 1024
        ):
            raise ValueError
        return value
    except Exception:
        raise ValueError("generation_request_denied") from None


@dataclass(frozen=True, repr=False)
class GenerationServiceConfig:
    target: str
    publication_config_file: str
    publication_config_sha256: str
    development_config_file: str
    development_config_sha256: str
    llm_config_file: str
    llm_config_sha256: str
    llm_key_file: str
    host_socket: str
    host_credential_file: str

    @classmethod
    def from_file(cls, path, *, expected_uid=0):
        try:
            value = json.loads(
                _secure_read(os.fspath(path), expected_uid), object_pairs_hook=_unique
            )
            if (
                not isinstance(value, dict)
                or set(value) != {"version", *cls.__dataclass_fields__}
                or type(value["version"]) is not int
                or value["version"] != 1
            ):
                raise ValueError
            if (
                not isinstance(value["target"], str)
                or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value["target"]) is None
            ):
                raise ValueError
            for prefix in ("publication", "development", "llm"):
                raw = _secure_read(value[prefix + "_config_file"], expected_uid)
                if hashlib.sha256(raw).hexdigest() != value[prefix + "_config_sha256"]:
                    raise ValueError
            for key in ("llm_key_file", "host_credential_file"):
                _secure_read(value[key], expected_uid)
            if (
                not isinstance(value["host_socket"], str)
                or not Path(value["host_socket"]).is_absolute()
            ):
                raise ValueError
            return cls(**{key: item for key, item in value.items() if key != "version"})
        except Exception:
            raise ValueError("generation_configuration_denied") from None

    def __repr__(self):
        return "GenerationServiceConfig([protected])"


def _document(path, digest):
    raw = _secure_read(path, 0)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("generation_configuration_denied")
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError("generation_configuration_denied")
    return value


def _current_main(gateway) -> str:
    value = gateway.github._api_json("GET", f"/repos/{gateway.repository}/git/ref/heads/main")
    if (
        not isinstance(value, dict)
        or value.get("ref") != "refs/heads/main"
        or not isinstance(value.get("object"), dict)
        or value["object"].get("type") != "commit"
        or not isinstance(value["object"].get("sha"), str)
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["object"]["sha"]) is None
    ):
        raise ValueError("baseline_unavailable")
    return value["object"]["sha"]


async def execute_generation(request: dict, *, gateway, development, host):
    # Revalidate even when called in process, not only through the CLI parser.
    request = parse_request(json.dumps(request, allow_nan=False).encode())
    if gateway.store.get_submission(request["request_id"]) is not None:
        published = await asyncio.to_thread(
            gateway.resume_request,
            request["request_id"],
            specification=request["specification"],
            pull_request_title=request["title"],
            pull_request_body=request["body"],
            synthetic_examples=request["synthetic_examples"],
        )
        return _publication_receipt(request["request_id"], published)
    observation = await host.current_state(gateway.policy.target)
    if not observation.healthy or observation.policy_digest != gateway.policy.digest:
        raise ValueError("baseline_unavailable")
    base = await asyncio.to_thread(_current_main, gateway)
    if base != development.runner.baseline_commit:
        raise ValueError("baseline_image_not_prepared")
    facts = await asyncio.to_thread(
        collect_release_bundle,
        gateway.repository_path,
        observation.identity.git_sha,
        base,
        base,
        gateway.policy,
    )
    if not facts.base_is_descendant_of_deployed:
        raise ValueError("baseline_unavailable")
    generated = GenerationRequest(
        specification=request["specification"],
        synthetic_examples=request["synthetic_examples"],
        base_commit=base,
        policy_digest=gateway.policy.digest,
    )
    published = await gateway.generate_and_publish(
        development=development,
        generation_request=generated,
        request_id=request["request_id"],
        deployed_commit=observation.identity.git_sha,
        pull_request_title=request["title"],
        pull_request_body=request["body"],
        commit_timestamp=int(time.time()),
    )
    return _publication_receipt(request["request_id"], published)


def _publication_receipt(request_id, published):
    return {
        "request_id": request_id,
        "pull_request_number": published.pull_request.pull_request_number,
        "head_sha": published.publication.head_commit,
    }


async def run(config: GenerationServiceConfig, request: dict):
    environment = {
        "RELEASE_CONTROL_PUBLICATION_CONFIG_PATH": config.publication_config_file,
        "RELEASE_CONTROL_PUBLICATION_CONFIG_SHA256": config.publication_config_sha256,
        "RELEASE_CONTROL_PUBLICATION_MODE": "gateway",
    }
    publication_config = ProtectedPublicationConfig.from_environment(environment)
    gateway = None
    provider = None
    try:
        dev = _document(config.development_config_file, config.development_config_sha256)
        if (
            set(dev)
            != {
                "version",
                "sandbox",
                "worker_manifest_file",
                "worker_source_file",
                "harness_source_file",
            }
            or dev["version"] != 1
            or not isinstance(dev["sandbox"], dict)
        ):
            raise ValueError("generation_configuration_denied")
        sandbox = SandboxConfig(
            **{**dev["sandbox"], "resources": ResourcePolicy(**dev["sandbox"]["resources"])}
        )
        worker = _secure_read(dev["worker_source_file"], 0)
        manifest = WorkerManifest.from_bytes(
            _secure_read(dev["worker_manifest_file"], 0),
            expected_manifest_digest=sandbox.worker_manifest_digest,
            worker_source=worker,
        )
        if (
            manifest.version != 2
            or hashlib.sha256(_secure_read(dev["harness_source_file"], 0)).hexdigest()
            != manifest.test_harness_digest
        ):
            raise ValueError("generation_configuration_denied")
        llm = _document(config.llm_config_file, config.llm_config_sha256)
        if (
            set(llm) != {"provider", "base_url", "model", "timeout_seconds", "effort"}
            or llm["provider"] != "openai_compat"
            or not isinstance(llm["model"], str)
            or not 1 <= len(llm["model"]) <= 128
            or type(llm["timeout_seconds"]) is not int
            or not 1 <= llm["timeout_seconds"] <= 120
            or llm["effort"] not in {"low", "medium", "high"}
        ):
            raise ValueError("generation_configuration_denied")
        origin = urlsplit(llm["base_url"])
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
        ):
            raise ValueError("generation_configuration_denied")
        key = _secure_read(config.llm_key_file, 0).decode().strip()
        provider = build_provider(
            LLMSettings(
                LLM_PROVIDER=llm["provider"],
                LLM_BASE_URL=llm["base_url"],
                LLM_MODEL=llm["model"],
                LLM_API_KEY=SecretStr(key),
                LLM_MAX_TOKENS=4096,
                LLM_TIMEOUT_S=llm["timeout_seconds"],
                LLM_EFFORT=llm["effort"],
                LLM_VISION=False,
                LLM_ANTHROPIC_FALLBACKS=False,
            )
        )
        gateway, _ = build_publication_gateway(environment)
        if (
            gateway.policy.target != config.target
            or sandbox.policy_digest != gateway.policy.digest
            or sandbox.baseline_commit != publication_config.values["baseline_commit"]
        ):
            raise ValueError("generation_configuration_denied")

        def scan(report):
            return (
                gateway.publisher._scanner(ScanInput("development-report", report))
                is ScanVerdict.CLEAN
            )

        development = DevelopmentController(
            provider=provider,
            installed_policy=gateway.policy,
            runner=DockerSandboxRunner(sandbox),
            report_scanner=scan,
            base_commit_verifier=lambda base, digest: (
                digest == gateway.policy.digest and _current_main(gateway) == base
            ),
        )
        host = ProtectedHostClient(
            socket_path=Path(config.host_socket),
            credential=_secure_read(config.host_credential_file, 0).decode().strip(),
        )
        return await execute_generation(
            request, gateway=gateway, development=development, host=host
        )
    finally:
        if gateway is not None:
            gateway.close()
        close = getattr(getattr(provider, "_client", None), "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="chatcore-release-generate")
    parser.add_argument("--config", default="/etc/chatcore-release-control/generation.json")
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise ValueError
        config = GenerationServiceConfig.from_file(args.config)
        request = parse_request(_secure_read(args.request, 0))
        result = asyncio.run(run(config, request))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print("generation_unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
