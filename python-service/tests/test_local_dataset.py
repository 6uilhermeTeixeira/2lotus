import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.local_dataset import DatasetError, DatasetRelease, file_sha256


class LocalDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def publish(self, version: str = "release-1", **manifest_overrides: object) -> Path:
        release = self.root / "releases" / version
        release.mkdir(parents=True, exist_ok=True)
        manifest = {
            "dataset_version": version,
            "release_status": "published",
            "schema_version": "xauusd-ticks-v1",
            "partitions": [],
        }
        manifest.update(manifest_overrides)
        if manifest.get("release_status") is None:
            manifest.pop("release_status")
        (release / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return release

    def test_file_sha256_reads_complete_file(self) -> None:
        source = self.root / "payload.bin"
        payload = b"lotus" * 300_000
        source.write_bytes(payload)
        self.assertEqual(file_sha256(source), hashlib.sha256(payload).hexdigest())

    def test_open_resolves_current_published_release(self) -> None:
        self.publish(status="ignored")
        pointer = self.root / "releases" / "CURRENT"
        pointer.write_text("release-1\n", encoding="utf-8")
        release = DatasetRelease.open(self.root)
        self.assertEqual(release.version, "release-1")
        self.assertEqual(release.root, self.root.resolve())

    def test_open_accepts_legacy_complete_status(self) -> None:
        self.publish(release_status=None, status="complete")
        release = DatasetRelease.open(self.root, "release-1")
        self.assertEqual(release.version, "release-1")

    def test_open_rejects_missing_pointer_and_unsafe_versions(self) -> None:
        with self.assertRaisesRegex(DatasetError, "CURRENT dataset pointer"):
            DatasetRelease.open(self.root)
        for version in ("", "../escape", "nested/version"):
            with self.subTest(version=version), self.assertRaisesRegex(DatasetError, "invalid dataset version"):
                DatasetRelease.open(self.root, version)

    def test_open_rejects_missing_or_inconsistent_manifest(self) -> None:
        with self.assertRaisesRegex(DatasetError, "release not found"):
            DatasetRelease.open(self.root, "missing")

        self.publish(dataset_version="another")
        with self.assertRaisesRegex(DatasetError, "does not match"):
            DatasetRelease.open(self.root, "release-1")

    def test_open_rejects_incomplete_and_unknown_schema(self) -> None:
        self.publish(release_status="draft")
        with self.assertRaisesRegex(DatasetError, "not complete"):
            DatasetRelease.open(self.root, "release-1")

        (self.root / "releases" / "release-1" / "manifest.json").unlink()
        self.publish(release_status="published", schema_version="unknown")
        with self.assertRaisesRegex(DatasetError, "unsupported dataset schema"):
            DatasetRelease.open(self.root, "release-1")

    def test_partitions_validate_entries_location_and_hash(self) -> None:
        partition = self.root / "data" / "ticks.parquet"
        partition.parent.mkdir()
        partition.write_bytes(b"parquet-placeholder")
        digest = file_sha256(partition)
        release = DatasetRelease(
            self.root,
            "release-1",
            {"partitions": [{"path": "data/ticks.parquet", "sha256": digest}]},
        )
        self.assertEqual(release.partitions(verify_hashes=True), [partition.resolve()])

        for entry, message in (("invalid", "invalid partition entry"), ({"path": "missing"}, "partition is missing")):
            with self.subTest(entry=entry), self.assertRaisesRegex(DatasetError, message):
                DatasetRelease(self.root, "release-1", {"partitions": [entry]}).partitions()

        bad_hash = DatasetRelease(self.root, "release-1", {"partitions": [{"path": "data/ticks.parquet", "sha256": "bad"}]})
        with self.assertRaisesRegex(DatasetError, "hash mismatch"):
            bad_hash.partitions(verify_hashes=True)

    def test_query_ticks_validates_bounds_and_empty_release(self) -> None:
        release = DatasetRelease(self.root, "release-1", {"partitions": []})
        for start, end, limit in ((2, 1, 1), (1, 1, 1), (1, 2, 0)):
            with self.subTest(start=start, end=end, limit=limit), self.assertRaisesRegex(DatasetError, "invalid tick query"):
                release.query_ticks(start, end, limit=limit)
        self.assertEqual(release.query_ticks(1, 2), [])

    def test_query_ticks_executes_parameterized_query_and_closes_connection(self) -> None:
        partition = self.root / "ticks.parquet"
        partition.touch()
        release = DatasetRelease(self.root, "release-1", {"partitions": [{"path": "ticks.parquet"}]})
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [(10, 1.0, 1.1, 2.0, 3.0)]
        fake_duckdb = SimpleNamespace(connect=MagicMock(return_value=connection))
        with patch.dict(sys.modules, {"duckdb": fake_duckdb}):
            rows = release.query_ticks(10, 20, limit=7)
        self.assertEqual(rows, [(10, 1.0, 1.1, 2.0, 3.0)])
        parameters = connection.execute.call_args.args[1]
        self.assertEqual(parameters, [[str(partition.resolve())], 10, 20, 7])
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
