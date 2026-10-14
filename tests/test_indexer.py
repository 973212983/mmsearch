import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from mmsearch import config, media
from mmsearch.indexer import Indexer, scan
from mmsearch.store import VectorStore


def unit(key: bytes) -> np.ndarray:
    rng = np.random.default_rng(int(hashlib.md5(key).hexdigest()[:8], 16))
    v = rng.standard_normal(768).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeEmbedder:
    """按内容生成确定的向量，并记录每次编码了多少条。"""

    def __init__(self):
        self.calls = []

    def encode_titled(self, docs):
        self.calls.append(("text", len(docs)))
        return np.stack([unit(f"{t}|{c}".encode()) for t, c in docs])

    def encode_images(self, imgs):
        self.calls.append(("image", len(imgs)))
        return np.stack([unit(im.tobytes()) for im in imgs])


class IndexerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "docs"
        self.root.mkdir()
        self.store = VectorStore(Path(self.tempdir.name) / "index.db")
        self.store.add_folder(str(self.root))
        self.emb = FakeEmbedder()

    def tearDown(self):
        self.store.db.close()
        self.tempdir.cleanup()

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def run_index(self) -> list:
        self.emb.calls = []
        Indexer(self.store, self.emb).run()
        return self.emb.calls

    def indexed(self) -> set[str]:
        return {os.path.relpath(p, self.root) for p in self.store.file_states()}

    def test_unchanged_moved_copied_and_touched_files_are_not_encoded_again(self):
        a = self.write("a/notes.txt", "会议记录：下周三发布")
        self.write("b/plan.md", "# 计划\n先做索引提速")
        self.assertTrue(self.run_index())

        self.assertEqual([], self.run_index())

        os.makedirs(self.root / "moved")
        shutil.move(a, self.root / "moved" / "notes.txt")
        shutil.copy2(self.root / "b" / "plan.md", self.root / "plan.md")
        os.utime(self.root / "b" / "plan.md", (1_000_000_000, 1_000_000_000))
        self.assertEqual([], self.run_index())
        self.assertEqual({os.path.join("moved", "notes.txt"), os.path.join("b", "plan.md"), "plan.md"},
                         self.indexed())
        self.assertEqual(3, len(self.store))
        self.assertEqual(1_000_000_000, self.store.file_states()[str(self.root / "b" / "plan.md")][1])

    def test_renamed_or_edited_text_is_encoded_again(self):
        self.write("notes.txt", "第一版")
        self.run_index()

        os.rename(self.root / "notes.txt", self.root / "renamed.txt")  # 文件名是向量的一部分（title）
        self.assertEqual([("text", 1)], self.run_index())
        self.write("renamed.txt", "第二版")
        self.assertEqual([("text", 1)], self.run_index())
        self.assertEqual({"renamed.txt"}, self.indexed())

    def test_small_text_files_share_one_request_batch(self):
        for i in range(6):
            self.write(f"n{i}.txt", f"小文件 {i}")

        self.assertEqual([("text", 6)], self.run_index())
        self.assertEqual(6, len(self.store))

    def test_identical_images_in_one_batch_are_encoded_once(self):
        Image.new("RGB", (64, 48), (200, 30, 30)).save(self.root / "red.png")
        shutil.copy2(self.root / "red.png", self.root / "red copy.png")
        Image.new("RGB", (64, 48), (30, 30, 200)).save(self.root / "blue.png")

        self.assertEqual([("image", 2)], self.run_index())
        self.assertEqual({"red.png", "red copy.png", "blue.png"}, self.indexed())
        self.assertEqual(3, len(self.store))

    def test_scan_skips_hidden_and_tool_directories(self):
        self.write("keep.txt", "x")
        self.write(".git/config.txt", "x")
        self.write("node_modules/pkg/readme.md", "x")
        self.write("sub/data.unknown", "x")
        self.write("sub/deep/photo.jpg", "not really a jpeg")

        found = {os.path.relpath(p, self.root): (mod, st.st_size) for p, mod, st in scan([str(self.root)])}

        self.assertEqual({"keep.txt": ("text", 1), os.path.join("sub", "deep", "photo.jpg"): ("image", 17)}, found)


class DataFileChunkTests(unittest.TestCase):
    def test_data_files_keep_head_middle_and_tail_chunks(self):
        n = config.DATA_CHUNKS_EACH
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"L{i:03d} " + "x" * 1100 for i in range(100)]  # 每行正好成一块
            for ext in (".log", ".txt"):
                (Path(tmp) / f"run{ext}").write_text("\n".join(lines), encoding="utf-8")
            data = media.file_chunks(Path(tmp) / "run.log")
            prose = media.file_chunks(Path(tmp) / "run.txt")

        marks = [int(c.split("L")[-1][:3]) for c in data]
        mid = (100 - n) // 2
        self.assertEqual(list(range(n)) + list(range(mid, mid + n)) + list(range(100 - n, 100)), marks)
        self.assertEqual(100, len(prose))


if __name__ == "__main__":
    unittest.main()
