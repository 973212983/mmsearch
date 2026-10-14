import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mmsearch import config, downloader
from mmsearch.store import VectorStore


class ModelSourceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.dirs = mock.patch.object(config, "MODEL_DIRS", [self.root / "app", self.root / "data"])
        self.dirs.start()

    def tearDown(self):
        self.dirs.stop()
        config.set_model_source(None)
        self.tempdir.cleanup()

    def touch(self, path: Path, size: int = 1) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_check_rejects_bad_input(self):
        ok = {"kind": "repo", "repo": "someone/embeddinggemma-2-GGUF", "model": "q4/model.gguf", "mmproj": "mmproj.gguf"}
        self.assertEqual(config.check_model_source(ok), "")
        self.assertTrue(config.check_model_source({**ok, "repo": "no-slash"}))
        self.assertTrue(config.check_model_source({**ok, "model": "../model.gguf"}))
        self.assertTrue(config.check_model_source({**ok, "model": "model.bin"}))
        self.assertTrue(config.check_model_source({**ok, "mmproj": "q4/model.gguf"}))
        self.assertTrue(config.check_model_source({"kind": "local", "model": "relative.gguf", "mmproj": "C:/m.gguf"}))

    def test_official_kind_follows_current_official_files(self):
        self.assertEqual(config.check_model_source({"kind": "official"}), "")
        with mock.patch.object(config, "OFFICIAL_SOURCE", {**config.OFFICIAL_SOURCE, "model": "q4.gguf"}):
            config.set_model_source({"kind": "official"})
            self.assertEqual(config.model_source()["model"], "q4.gguf")  # 环境变量换了量化也能生效
            self.assertEqual(config.model_download_dir(), self.root / "app")

    def test_other_repo_uses_own_subdir_and_ignores_official_files(self):
        self.touch(self.root / "app" / config.GGUF_NAME)
        self.touch(self.root / "app" / config.MMPROJ_NAME)
        config.set_model_source({"kind": "repo", "repo": "someone/mirror", "model": config.GGUF_NAME,
                                 "mmproj": config.MMPROJ_NAME})
        self.assertEqual(len(config.missing_models()), 2)  # 同名的官方文件不算数
        self.assertEqual(config.model_download_dir(), self.root / "app" / "someone__mirror")
        self.assertEqual(config.known_size(config.GGUF_NAME), 0)  # 非官方仓库不按官方大小校验

        self.touch(self.root / "app" / "someone__mirror" / config.GGUF_NAME)
        self.assertEqual(config.missing_models(), [config.MMPROJ_NAME])

    def test_download_urls_keep_repo_subpath_but_save_basename(self):
        config.set_model_source({"kind": "repo", "repo": "someone/mirror", "model": "q4 k/model.gguf",
                                 "mmproj": "mmproj.gguf"})
        jobs = downloader.model_urls("https://hf.example")
        self.assertEqual(jobs[0], ("https://hf.example/someone/mirror/resolve/main/q4%20k/model.gguf", "model.gguf"))
        self.assertEqual(jobs[1][1], "mmproj.gguf")

    def test_local_source(self):
        model = self.touch(self.root / "elsewhere" / "m.gguf", 3)
        mmproj = self.touch(self.root / "elsewhere" / "p.gguf", 5)
        config.set_model_source({"kind": "local", "model": str(model), "mmproj": str(mmproj)})
        self.assertEqual(config.missing_models(), [])
        self.assertEqual(config.model_files(), {"model": model, "mmproj": mmproj})
        self.assertEqual(downloader.model_urls("https://hf.example"), [])
        self.assertEqual(config.model_fingerprint(), "model=m.gguf:3;mmproj=p.gguf:5")
        mmproj.unlink()
        self.assertEqual(config.missing_models(), [str(mmproj)])


class StoreMetaTests(unittest.TestCase):
    def test_meta_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            store = VectorStore(Path(d) / "index.db")
            self.assertIsNone(store.get_meta("model"))
            store.set_meta("model", "a")
            store.set_meta("model", "b")
            self.assertEqual(store.get_meta("model"), "b")
            store.db.close()


if __name__ == "__main__":
    unittest.main()
