"""Downloads the LongMemEval-s dataset (oracle + full history variants).

Usage: python scripts/download_longmemeval.py [--out data/]

Source: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
(the maintainers' own cleaned release -- see LongMemEval's README, "we have
further cleaned up the history sessions to prevent interference on answer
correctness").
"""

from __future__ import annotations

import argparse
from pathlib import Path

import requests

BASE_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main"
)
FILES = [
    "longmemeval_oracle.json",  # evidence sessions only -- fast to iterate on
    "longmemeval_s_cleaned.json",  # full ~115k-token, ~40-session history
]


def download(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename in FILES:
        dest = out_dir / filename
        if dest.exists():
            print(f"skip (already present): {dest}")
            continue
        url = f"{BASE_URL}/{filename}"
        print(f"downloading {url}")
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        dest.write_bytes(response.content)
        print(f"wrote {dest} ({len(response.content):,} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("data/raw"))
    args = parser.parse_args()
    download(args.out)
