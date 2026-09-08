from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


CONTRACT = "lotus-strategy-v1"
SOURCE_FILES = {"strategy.py", "indicators.py", "regime.py", "adaptive.py", "schemas.py", "config.py"}


class PromotionError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if member.isdir():
            continue
        if not member.isfile() or path.is_absolute() or ".." in path.parts:
            raise PromotionError(f"unsafe bundle member: {member.name}")
        normalized = path.as_posix()
        allowed_source = (
            len(path.parts) == 2 and path.parts[0] == "source" and path.parts[1] in SOURCE_FILES
        ) or (
            len(path.parts) == 3 and path.parts[:2] == ("source", "strategies")
            and path.parts[2] in {"__init__.py", "core.py", "tick.py", "portfolio.py", "router.py"}
        )
        allowed = normalized == "manifest.json" or allowed_source
        if not allowed or normalized in members:
            raise PromotionError(f"unexpected bundle member: {member.name}")
        members[normalized] = member
    if "manifest.json" not in members:
        raise PromotionError("bundle has no manifest.json")
    return members


def _read_manifest(archive: tarfile.TarFile, member: tarfile.TarInfo) -> dict[str, Any]:
    stream = archive.extractfile(member)
    if stream is None:
        raise PromotionError("cannot read bundle manifest")
    try:
        manifest = json.loads(stream.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PromotionError("invalid bundle manifest") from exc
    required = {"contract", "strategy_id", "deployment_id", "version", "dataset_version", "files"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise PromotionError("bundle manifest is incomplete")
    if manifest["contract"] != CONTRACT:
        raise PromotionError(f"unsupported contract: {manifest['contract']}")
    for field in ("strategy_id", "deployment_id", "version", "dataset_version"):
        value = manifest[field]
        if not isinstance(value, str) or not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value):
            raise PromotionError(f"invalid {field}")
    if not isinstance(manifest["files"], dict):
        raise PromotionError("manifest files must be an object")
    return manifest


def import_bundle(bundle_path: Path, registry_root: Path) -> dict[str, Any]:
    bundle_path = bundle_path.resolve(strict=True)
    registry_root = registry_root.resolve()
    registry_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path, "r:gz") as archive:
        members = _safe_members(archive)
        manifest = _read_manifest(archive, members["manifest.json"])
        expected_files = manifest["files"]
        actual_files = {name.removeprefix("source/") for name in members if name.startswith("source/")}
        if set(expected_files) != actual_files or not actual_files:
            raise PromotionError("source files do not match manifest")

        destination = registry_root / manifest["deployment_id"] / manifest["version"]
        if destination.exists():
            raise PromotionError("strategy version is already imported")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=destination.parent))
        try:
            (staging / "source").mkdir()
            for filename in sorted(actual_files):
                source = archive.extractfile(members[f"source/{filename}"])
                if source is None:
                    raise PromotionError(f"cannot read {filename}")
                target = staging / "source" / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                expected = expected_files[filename]
                expected_hash = expected.get("sha256") if isinstance(expected, dict) else expected
                if expected_hash != _sha256(target):
                    raise PromotionError(f"checksum mismatch: {filename}")
            receipt = {
                **manifest,
                "imported_at_utc": datetime.now(timezone.utc).isoformat(),
                "bundle_sha256": _sha256(bundle_path),
                "active": False,
                "live_canary_armed": False,
            }
            (staging / "manifest.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return receipt


def list_promotions(registry_root: Path) -> list[dict[str, Any]]:
    if not registry_root.exists():
        return []
    promotions = []
    for path in sorted(registry_root.glob("*/*/manifest.json")):
        try:
            promotions.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return promotions
