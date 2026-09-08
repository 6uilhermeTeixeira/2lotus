"""Read immutable datasets published locally by 1lotus."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path


SUPPORTED_SCHEMAS = {"xauusd-ticks-v1"}


class DatasetError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class DatasetRelease:
    root: Path
    version: str
    manifest: dict[str, object]

    @classmethod
    def open(cls, root: Path, requested_version: str = "CURRENT") -> "DatasetRelease":
        root = root.expanduser().resolve()
        if requested_version == "CURRENT":
            pointer = root / "releases/CURRENT"
            if not pointer.is_file():
                raise DatasetError("1lotus CURRENT dataset pointer is unavailable")
            requested_version = pointer.read_text(encoding="utf-8").strip()
        if not requested_version or "/" in requested_version or ".." in requested_version:
            raise DatasetError("invalid dataset version")
        manifest_path = root / "releases" / requested_version / "manifest.json"
        if not manifest_path.is_file():
            raise DatasetError(f"dataset release not found: {requested_version}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("dataset_version") != requested_version:
            raise DatasetError("dataset version does not match its manifest")
        release_status = manifest.get("release_status", manifest.get("status"))
        if release_status not in {"published", "complete"}:
            raise DatasetError("dataset release is not complete")
        if manifest.get("schema_version") not in SUPPORTED_SCHEMAS:
            raise DatasetError(f"unsupported dataset schema: {manifest.get('schema_version')}")
        return cls(root=root, version=requested_version, manifest=manifest)

    def partitions(self, *, verify_hashes: bool = False) -> list[Path]:
        paths: list[Path] = []
        for row in self.manifest.get("partitions", []):
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise DatasetError("invalid partition entry")
            path = (self.root / row["path"]).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise DatasetError(f"partition is missing or outside data root: {row['path']}")
            if verify_hashes and file_sha256(path) != row.get("sha256"):
                raise DatasetError(f"partition hash mismatch: {row['path']}")
            paths.append(path)
        return paths

    def query_ticks(self, from_ms: int, to_ms: int, *, limit: int = 1_000_000) -> list[tuple]:
        if from_ms >= to_ms or limit < 1:
            raise DatasetError("invalid tick query bounds")
        import duckdb

        files = [str(path) for path in self.partitions()]
        if not files:
            return []
        connection = duckdb.connect(":memory:")
        try:
            return connection.execute(
                """SELECT timestamp_ms,bid,ask,bid_volume,ask_volume
                   FROM read_parquet(?, hive_partitioning=true)
                   WHERE timestamp_ms >= ? AND timestamp_ms < ?
                   ORDER BY timestamp_ms LIMIT ?""",
                [files, from_ms, to_ms, limit],
            ).fetchall()
        finally:
            connection.close()
