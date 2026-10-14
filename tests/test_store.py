import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mmsearch.store import VectorStore


def vector(axis: int) -> np.ndarray:
    value = np.zeros((1, 768), dtype=np.float32)
    value[0, axis] = 1.0
    return value


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = VectorStore(Path(self.tempdir.name) / "index.db")

    def tearDown(self):
        self.store.db.close()
        self.tempdir.cleanup()

    def put(self, name: str, axis: int, chunks: int = 1):
        items = [{"kind": "text", "snippet": f"chunk {i}"} for i in range(chunks)]
        vecs = np.repeat(vector(axis), chunks, axis=0)
        self.store.put_file(name, 1.0, 1, "text", items, vecs)

    def assert_memory_matches_database(self):
        database_ids = {row[0] for row in self.store.db.execute("SELECT id FROM items")}
        self.assertEqual(database_ids, set(map(int, self.store.ids)))
        self.assertEqual(len(database_ids), len(self.store))
        self.assertEqual(database_ids, set(self.store._positions))
        for item_id, pos in self.store._positions.items():
            self.assertEqual(item_id, int(self.store.ids[pos]))


class VectorStoreUpdateTests(StoreTestCase):
    def test_replacing_file_reuses_dense_memory_without_stale_items(self):
        self.put("a.txt", 0)
        self.put("b.txt", 1, chunks=2)
        self.put("c.txt", 2)
        buffers = tuple(map(id, self.store._buf))
        old_ids = {row[0] for row in self.store.db.execute(
            "SELECT items.id FROM items JOIN files ON files.id=items.file_id WHERE files.path='b.txt'")}

        self.put("b.txt", 3)

        self.assertEqual(3, len(self.store))
        self.assertEqual(buffers, tuple(map(id, self.store._buf)))
        self.assertTrue(old_ids.isdisjoint(set(map(int, self.store.ids))))
        self.assert_memory_matches_database()
        hit = self.store.search(vector(3)[0], k=1)[0]
        self.assertEqual("b.txt", hit.path)

    def test_delete_files_handles_rows_moved_by_an_earlier_removal(self):
        for index, name in enumerate(("a.txt", "b.txt", "c.txt", "d.txt")):
            self.put(name, index)
        file_ids = [row[0] for row in self.store.db.execute(
            "SELECT id FROM files WHERE path IN ('a.txt', 'd.txt') ORDER BY path")]

        self.store.delete_files(file_ids)

        self.assert_memory_matches_database()
        self.assertEqual({"b.txt", "c.txt"}, {row[0] for row in self.store.db.execute("SELECT path FROM files")})

    def test_reopened_store_can_replace_and_delete_files(self):
        for index, name in enumerate(("a.txt", "b.txt", "c.txt")):
            self.put(name, index, chunks=2)
        self.store.db.close()
        self.store = VectorStore(Path(self.tempdir.name) / "index.db")  # 从数据库加载的内存矩阵

        self.put("a.txt", 3)
        self.store.delete_files([self.store.file_states()["b.txt"][0]])

        self.assert_memory_matches_database()
        self.assertEqual("a.txt", self.store.search(vector(3)[0], k=1)[0].path)

    def test_replacing_file_with_error_removes_its_vectors(self):
        self.put("broken.txt", 0, chunks=3)

        self.store.put_file("broken.txt", 2.0, 2, "text", [], None, error="decode failed")

        self.assertEqual(0, len(self.store))
        self.assert_memory_matches_database()
        self.assertEqual([("broken.txt", "decode failed")], self.store.failed_files())


class VectorStoreReuseTests(StoreTestCase):
    def put_hashed(self, path: str, axis: int, fp: str, chunks: int = 1, error: str | None = None):
        items = [] if error else [{"kind": "text", "snippet": f"chunk {i}"} for i in range(chunks)]
        vecs = None if error else np.repeat(vector(axis), chunks, axis=0)
        self.store.put_file(path, 1.0, 10, "text", items, vecs, error=error, fp=fp)

    def paths_for(self, axis: int) -> set[str]:
        return {h.path for h in self.store.search(vector(axis)[0], k=10, dedup_files=False)
                if h.score > 0.99}

    def test_reuse_copies_items_for_a_moved_or_copied_file(self):
        self.put_hashed(r"C:\a\notes.txt", 0, "h1", chunks=2)

        self.assertTrue(self.store.reuse(r"C:\b\notes.txt", 2.0, 10, "text", "h1", "notes.txt"))

        self.assertEqual(4, len(self.store))
        self.assert_memory_matches_database()
        self.assertEqual({r"C:\a\notes.txt", r"C:\b\notes.txt"}, self.paths_for(0))
        snippets = [r[0] for r in self.store.db.execute(
            "SELECT snippet FROM items JOIN files ON files.id=items.file_id WHERE path=? ORDER BY items.id",
            (r"C:\b\notes.txt",))]
        self.assertEqual(["chunk 0", "chunk 1"], snippets)

    def test_reuse_of_same_path_only_updates_mtime(self):
        self.put_hashed(r"C:\a\notes.txt", 0, "h1", chunks=2)
        item_ids = set(map(int, self.store.ids))

        self.assertTrue(self.store.reuse(r"C:\a\notes.txt", 5.0, 10, "text", "h1", "notes.txt"))

        self.assertEqual(item_ids, set(map(int, self.store.ids)))
        self.assertEqual(5.0, self.store.file_states()[r"C:\a\notes.txt"][1])

    def test_text_is_only_reused_under_the_same_file_name(self):
        self.put_hashed(r"C:\a\notes.txt", 0, "h1")

        self.assertFalse(self.store.reuse(r"C:\a\renamed.txt", 2.0, 10, "text", "h1", "renamed.txt"))
        self.assertTrue(self.store.reuse(r"C:\a\photo.jpg", 2.0, 10, "text", "h1", None))

    def test_failed_files_are_not_reused(self):
        self.put_hashed(r"C:\a\broken.txt", 0, "h1", error="decode failed")

        self.assertFalse(self.store.reuse(r"C:\b\broken.txt", 2.0, 10, "text", "h1", "broken.txt"))

    def test_hidden_files_leave_search_but_stay_reusable_until_deleted(self):
        self.put_hashed(r"C:\a\old.txt", 0, "h1")
        self.put_hashed(r"C:\a\keep.txt", 1, "h2")
        old_id = self.store.file_states()[r"C:\a\old.txt"][0]

        self.store.hide_files([old_id])

        self.assertEqual(set(), self.paths_for(0))
        self.assertTrue(self.store.reuse(r"C:\b\old.txt", 2.0, 10, "text", "h1", "old.txt"))
        self.assertEqual({r"C:\b\old.txt"}, self.paths_for(0))
        self.store.delete_files([old_id])
        self.assertNotIn(r"C:\a\old.txt", self.store.file_states())
        self.assert_memory_matches_database()


class VectorStoreMigrationTests(unittest.TestCase):
    def test_opening_an_old_database_adds_the_hash_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.db"
            old = sqlite3.connect(str(path))
            old.executescript("""
                CREATE TABLE folders(path TEXT PRIMARY KEY);
                CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, mtime REAL NOT NULL,
                                   size INTEGER NOT NULL, modality TEXT NOT NULL, error TEXT);
                CREATE TABLE items(id INTEGER PRIMARY KEY,
                                   file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                                   kind TEXT NOT NULL, start REAL, end REAL, snippet TEXT, vec BLOB NOT NULL);
                INSERT INTO files(path, mtime, size, modality) VALUES('old.txt', 1.0, 1, 'text');
            """)
            old.close()

            store = VectorStore(path)
            store.put_file("new.txt", 1.0, 1, "text", [{"kind": "text", "snippet": "x"}], vector(0), fp="h1")

            self.assertEqual({"old.txt": None, "new.txt": "h1"},
                             dict(store.db.execute("SELECT path, hash FROM files")))
            store.db.close()


if __name__ == "__main__":
    unittest.main()
