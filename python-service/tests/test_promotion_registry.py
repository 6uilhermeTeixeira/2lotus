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


if __name__ == "__main__":
    unittest.main()
