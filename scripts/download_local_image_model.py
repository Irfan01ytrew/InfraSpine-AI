"""Download the pinned, optional local Tiny-SD image model outside the source tree."""

from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download

MODEL_ID = "segmind/tiny-sd"
MODEL_REVISION = "cad0bd7495fa6c4bcca01b19a723dc91627fe84f"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path, help="Directory for the approximately 1 GB model")
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    result = snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=str(destination),
        allow_patterns=["model_index.json", "scheduler/*", "text_encoder/*", "tokenizer/*", "unet/*", "vae/*"],
    )
    print(f"Downloaded {MODEL_ID}@{MODEL_REVISION} to {result}")
    print(f"Set RAG_LOCAL_IMAGE_MODEL_PATH={result} in the application environment.")


if __name__ == "__main__":
    main()
