import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from app.promotion_registry import PromotionError, import_bundle, list_promotions


class PromotionRegistryTests(unittest.TestCase):
    def _bundle(self, root: Path, *, content: bytes = b"pass\n", checksum: str | None = None, extra: str | None = None) -> Path:
        bundle = root / "bundle.tar.gz"
        digest = checksum or hashlib.sha256(content).hexdigest()
        manifest = {
            "contract": "lotus-strategy-v1",
            "strategy_id": "bot-1",
            "deployment_id": "core-portfolio",
            "version": "1.0.0",
            "dataset_version": "dataset-1",
            "files": {"strategy.py": {"sha256": digest}},
        }
        with tarfile.open(bundle, "w:gz") as archive:
            encoded = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))
            info = tarfile.TarInfo("source/strategy.py")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
            if extra:
                info = tarfile.TarInfo(extra)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return bundle

    def _custom_bundle(self, root: Path, manifest: object | bytes | None, *, include_source: bool = True) -> Path:
        bundle = root / "custom.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            if manifest is not None:
                encoded = manifest if isinstance(manifest, bytes) else json.dumps(manifest).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(encoded)
                archive.addfile(info, io.BytesIO(encoded))
            if include_source:
                content = b"pass\n"
                info = tarfile.TarInfo("source/strategy.py")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return bundle

    def test_import_is_inactive_and_does_not_arm_canary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = import_bundle(self._bundle(root), root / "registry")
            self.assertFalse(receipt["active"])
            self.assertFalse(receipt["live_canary_armed"])
            self.assertEqual(len(list_promotions(root / "registry")), 1)

    def test_rejects_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(PromotionError, "checksum mismatch"):
                import_bundle(self._bundle(root, checksum="0" * 64), root / "registry")

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(PromotionError, "unsafe bundle member"):
                import_bundle(self._bundle(root, extra="../escape.py"), root / "registry")

    def test_rejects_missing_duplicate_and_unexpected_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(PromotionError, "no manifest"):
                import_bundle(self._custom_bundle(root, None), root / "registry")
            with self.assertRaisesRegex(PromotionError, "unexpected bundle member"):
                import_bundle(self._bundle(root, extra="source/unexpected.py"), root / "registry")
            with self.assertRaisesRegex(PromotionError, "unexpected bundle member"):
                import_bundle(self._bundle(root, extra="manifest.json"), root / "registry")

    def test_manifest_validation_rejects_malformed_contract_fields_and_files(self):
        valid = {
            "contract": "lotus-strategy-v1",
            "strategy_id": "bot-1",
            "deployment_id": "core-portfolio",
            "version": "1.0.0",
            "dataset_version": "dataset-1",
            "files": {"strategy.py": hashlib.sha256(b"pass\n").hexdigest()},
        }
        cases = [
            (b"{", "invalid bundle manifest"),
            ({}, "manifest is incomplete"),
            ({**valid, "contract": "other"}, "unsupported contract"),
            ({**valid, "strategy_id": "bad/id"}, "invalid strategy_id"),
            ({**valid, "files": []}, "files must be an object"),
            ({**valid, "files": {"indicators.py": "0" * 64}}, "source files do not match"),
        ]
        for index, (manifest, message) in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaisesRegex(PromotionError, message):
                    import_bundle(self._custom_bundle(root, manifest), root / "registry")

    def test_rejects_importing_same_strategy_version_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            import_bundle(bundle, root / "registry")
            with self.assertRaisesRegex(PromotionError, "already imported"):
                import_bundle(bundle, root / "registry")

    def test_list_promotions_handles_missing_root_and_corrupt_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(list_promotions(root / "missing"), [])
            corrupt = root / "registry" / "deployment" / "version" / "manifest.json"
            corrupt.parent.mkdir(parents=True)
            corrupt.write_text("{")
            self.assertEqual(list_promotions(root / "registry"), [])


if __name__ == "__main__":
    unittest.main()
