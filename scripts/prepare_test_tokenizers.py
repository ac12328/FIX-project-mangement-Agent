"""Prepare hash-pinned public tokenizer assets before enabling test egress guards.

This is dependency installation, not a test or an LLM call. Do not invoke it
from an application import or from a guarded test run. With --cache-dir, set
CUSTOM_TIKTOKEN_CACHE_DIR to the same directory before running consumers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Mapping
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
# Hashes are the expected_hash values in locked tiktoken 0.14.0.
ASSETS = (
    ("cl100k_base", "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
     "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"),
    ("o200k_base", "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken",
     "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"),
)


def tokenizer_cache_directory(env: Mapping[str, str]) -> Path:
    # LiteLLM overrides TIKTOKEN_CACHE_DIR with its custom cache option.
    configured = env.get("CUSTOM_TIKTOKEN_CACHE_DIR") or env.get("TIKTOKEN_CACHE_DIR")
    return Path(configured).resolve() if configured else ROOT / ".venv" / "test-tokenizers"


def download_asset(url: str) -> bytes:
    with urlopen(url, timeout=60) as response:
        return response.read()


def prepare_tokenizers(
    cache_dir: Path, fetch: Callable[[str], bytes] = download_asset,
) -> list[dict[str, object]]:
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for encoding, url, expected in ASSETS:
        cache_key = hashlib.sha1(url.encode()).hexdigest()
        destination = cache_dir / cache_key
        reused = destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected
        if not reused:
            contents = fetch(url)
            if hashlib.sha256(contents).hexdigest() != expected:
                raise ValueError(f"Tokenizer asset SHA-256 mismatch: {encoding}")
            temporary = cache_dir / f"{cache_key}.{uuid4().hex}.tmp"
            try:
                temporary.write_bytes(contents)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        records.append({"encoding": encoding, "url": url, "path": str(destination),
                        "sha256": expected, "bytes": destination.stat().st_size,
                        "reused": reused})
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=tokenizer_cache_directory(os.environ))
    args = parser.parse_args()
    print(json.dumps({"cache_dir": str(args.cache_dir.resolve()),
                      "assets": prepare_tokenizers(args.cache_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
