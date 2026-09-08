#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1] / "python-service"
sys.path.insert(0, str(SERVICE_ROOT))

from app.config import Settings  # noqa: E402
from app.promotion_registry import PromotionError, import_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Import an approved 2Lotus strategy bundle into 3Lotus")
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = import_bundle(args.bundle, Settings().strategy_registry_path)
    except (OSError, PromotionError) as exc:
        print(f"import rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
