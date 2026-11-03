"""Download the pinned FastEmbed model snapshot and verify Hugging Face SHA-256 metadata."""

from __future__ import annotations

import argparse
import hashlib
import os
from fnmatch import fnmatch
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

REPOSITORY = "onnx-community/embeddinggemma-300m-ONNX"
REVISION = "5090578d9565bb06545b4552f76e6bc2c93e4a66"
ALLOW_PATTERNS = [
    "onnx/model.onnx",
    "onnx/model.onnx_data",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "config.json",
    "tokenizer.model",
]


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    cache = Path(os.environ.get("EMBED_CACHE_DIR", "models/embeddinggemma-300m"))
    cache.mkdir(parents=True, exist_ok=True)
    info = HfApi().model_info(REPOSITORY, revision=REVISION, files_metadata=True)
    snapshot = Path(
        snapshot_download(
            repo_id=REPOSITORY,
            revision=REVISION,
            local_dir=cache,
            allow_patterns=ALLOW_PATTERNS,
        )
    )
    verified = 0
    for item in info.siblings:
        if not any(fnmatch(item.rfilename, pattern) for pattern in ALLOW_PATTERNS):
            continue
        expected = item.lfs.sha256 if item.lfs else None
        if expected is None:
            continue
        digest = hashlib.sha256((snapshot / item.rfilename).read_bytes()).hexdigest()
        if digest != expected:
            raise SystemExit(f"SHA-256 mismatch for pinned model file: {item.rfilename}")
        verified += 1
    if verified == 0:
        raise SystemExit("Hugging Face returned no SHA-256 file metadata to verify")
    print(f"Downloaded revision {REVISION}; SHA-256 verified {verified} model files in {cache}")


if __name__ == "__main__":
    main()
