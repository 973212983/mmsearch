import hashlib
import os
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
from PIL import Image

from mmsearch import config, media, watcher
from mmsearch.indexer import Indexer, _within_folders, scan
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

    def test_static_video_segments_reuse_the_last_encoded_vector(self):
        rng = np.random.default_rng(0)
        a = rng.integers(0, 255, (48, 64, 3), dtype=np.uint8)
        b = rng.integers(0, 255, (48, 64, 3), dtype=np.uint8)
        seg = lambda arr: [Image.fromarray(arr)] * 2
        cursor = a.copy()
        cursor[10:12, 10:12] = 255  # 录屏里光标动了一下：几乎没变
        units = [("video", 0, 30, seg(a)), ("video", 30, 60, seg(cursor)), ("video", 60, 90, seg(b)),
                 ("video", 90, 120, seg(a)), ("video_audio", 0, 30, np.ones(16000, np.float32))]

        class VideoEmbedder(FakeEmbedder):
            def encode_videos(self, segs):
                self.calls.append(("video", len(segs)))
                return np.stack([unit(segs[0][0].tobytes())])

            def encode_audios(self, clips):
                self.calls.append(("audio", len(clips)))
                return np.stack([unit(c.tobytes()) for c in clips])

        emb = VideoEmbedder()
        with unittest.mock.patch.object(Indexer, "_video_units", staticmethod(lambda p: (u for u in units))):
            items, vecs = Indexer(self.store, emb)._process(Path("clip.mp4"), "video")

        self.assertEqual([("video", 1), ("video", 1), ("video", 1), ("audio", 1)], emb.calls)  # 第 2 段复用了第 1 段
        self.assertEqual([0, 30, 60, 90, 0], [it["start"] for it in items])
        self.assertTrue(np.array_equal(vecs[0], vecs[1]))
        self.assertFalse(np.array_equal(vecs[1], vecs[2]))

    def test_scan_skips_hidden_and_tool_directories(self):
        self.write("keep.txt", "x")
        self.write(".git/config.txt", "x")
        self.write("node_modules/pkg/readme.md", "x")
        self.write("sub/data.unknown", "x")
        self.write("sub/deep/photo.jpg", "not really a jpeg")

        found = {os.path.relpath(p, self.root): (mod, st.st_size) for p, mod, st in scan([str(self.root)])}

        self.assertEqual({"keep.txt": ("text", 1), os.path.join("sub", "deep", "photo.jpg"): ("image", 17)}, found)

    def run_scoped(self, *scope) -> list:
        self.emb.calls = []
        Indexer(self.store, self.emb).run(scope={(str(self.root / d) if d else str(self.root), rec) for d, rec in scope})
        return self.emb.calls

    def test_scoped_run_only_touches_the_given_directories(self):
        self.write("a/x.txt", "一")
        self.write("b/y.txt", "二")
        self.run_index()
        self.write("a/new.txt", "三")
        self.write("b/new.txt", "四")
        os.remove(self.root / "b" / "y.txt")

        self.assertEqual([("text", 1)], self.run_scoped(("a", False)))
        # b 没在范围里：新文件还没处理，删掉的文件也还在
        self.assertEqual({os.path.join("a", "x.txt"), os.path.join("a", "new.txt"), os.path.join("b", "y.txt")},
                         self.indexed())

    def test_scoped_run_of_a_removed_directory_drops_its_files(self):
        self.write("old/deep/x.txt", "一")
        self.write("keep.txt", "二")
        self.run_index()
        shutil.rmtree(self.root / "old")

        self.assertEqual([], self.run_scoped(("old", True)))
        self.assertEqual({"keep.txt"}, self.indexed())

    def test_scoped_run_reuses_vectors_of_a_moved_directory(self):
        self.write("old/x.txt", "一")
        self.run_index()
        os.rename(self.root / "old", self.root / "new")

        self.assertEqual([], self.run_scoped(("old", True), ("new", True)))
        self.assertEqual({os.path.join("new", "x.txt")}, self.indexed())

    def test_scope_outside_added_folders_is_ignored(self):
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        (outside / "z.txt").write_text("外面", encoding="utf-8")

        Indexer(self.store, self.emb).run(scope={(str(outside), True)})
        self.assertEqual(set(), self.indexed())


class WatcherScopeTests(unittest.TestCase):
    def test_events_map_to_directories_to_rescan(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "newdir"))
        j = lambda *p: os.path.join(root, *p)
        self.assertEqual([(j("sub"), False)], watcher.scopes_for(root, watcher.MODIFIED, os.path.join("sub", "a.txt")))
        self.assertEqual([(j("newdir"), True)], watcher.scopes_for(root, watcher.ADDED, "newdir"))
        self.assertEqual([(j("gone"), True)], watcher.scopes_for(root, watcher.REMOVED, "gone"))
        self.assertEqual([], watcher.scopes_for(root, watcher.MODIFIED, "newdir"))  # 目录自身的修改由子项记录处理
        self.assertEqual([], watcher.scopes_for(root, watcher.ADDED, os.path.join(".git", "index.txt")))
        self.assertEqual([], watcher.scopes_for(root, watcher.ADDED, os.path.join("node_modules", "x", "a.js")))
        self.assertEqual([], watcher.scopes_for(root, watcher.ADDED, "~$报告.docx"))
        self.assertEqual([], watcher.scopes_for(root, watcher.ADDED, "setup.exe"))
        shutil.rmtree(root)

    def test_nested_scopes_are_merged(self):
        root = r"C:\data"
        scope = {(r"C:\data\a", True), (r"C:\data\a\b", False), (r"C:\data\a\c", True), (r"C:\data\x", False),
                 (r"C:\other", True)}
        self.assertEqual({(r"C:\data\a", True), (r"C:\data\x", False)}, _within_folders(scope, [root]))


class FolderWatcherTests(unittest.TestCase):
    def test_changes_are_reported_once_after_a_quiet_period(self):
        root = tempfile.mkdtemp()
        got, done = [], threading.Event()
        w = watcher.FolderWatcher(lambda scope: (got.append(scope), done.set()))
        old_quiet, watcher.QUIET = watcher.QUIET, 0.3
        try:
            w.set_folders([root])
            time.sleep(0.2)
            os.makedirs(os.path.join(root, "sub"))
            for i in range(5):
                Path(root, "sub", f"n{i}.txt").write_text(str(i), encoding="utf-8")
            Path(root, "skip.bin").write_bytes(b"x")
            self.assertTrue(done.wait(5))
            time.sleep(0.5)
        finally:
            watcher.QUIET = old_quiet
            w.stop()
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(1, len(got))
        self.assertIn((os.path.join(root, "sub"), False), got[0])


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
