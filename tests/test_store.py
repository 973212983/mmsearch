import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from mmsearch import config
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


class LexicalSearchTests(StoreTestCase):
    def put_doc(self, path: str, axis: int, snippet: str, kind: str = "text"):
        self.store.put_file(path, 1.0, 1, kind, [{"kind": kind, "snippet": snippet}], vector(axis))

    def test_exact_identifier_outranks_closer_vectors(self):
        self.put_doc(r"C:\docs\near.md", 0, "general notes")
        self.put_doc(r"C:\docs\api.md", 1, "calls acme.billing.fetchRemainingCredits")
        q = vector(0)[0] * 0.74 + vector(1)[0] * 0.67  # 差距小于 LEXICAL_BOOST

        self.assertEqual(r"C:\docs\near.md", self.store.search(q, k=1, boost=0)[0].path)
        hit = self.store.search(q, k=1, lex=self.store.lexical("fetchRemainingCredits"))[0]
        self.assertEqual(r"C:\docs\api.md", hit.path)
        self.assertTrue(hit.lexical)

    def test_file_name_match_pulls_in_files_outside_the_vector_candidates(self):
        self.put_doc(r"C:\pics\482913_receipt.png", 5, "482913_receipt.png", kind="image")
        self.put_doc(r"C:\docs\other.md", 0, "unrelated")

        hits = self.store.search(vector(0)[0], k=5, kinds={"image"}, lex=self.store.lexical("482913"))
        self.assertEqual([r"C:\pics\482913_receipt.png"], [h.path for h in hits])

    def test_terms_may_match_folder_but_one_must_match_the_file_name(self):
        self.put_doc(r"C:\proj\resetcounter\action.go", 0, "package x")
        self.put_doc(r"C:\proj\resetcounter\register.go", 1, "package x")
        self.put_doc(r"C:\proj\other\action.go", 2, "package y")

        lex = self.store.lexical("resetcounter action.go")
        paths = {self.store.item_path(i) for i in lex}
        self.assertEqual({r"C:\proj\resetcounter\action.go"}, paths)
        self.assertEqual({}, self.store.lexical("proj"))  # 只命中文件夹不算
        self.assertEqual({}, self.store.lexical("a o"))  # 单个字母不算关键词

    def test_two_char_chinese_words_match_text_anywhere_in_the_file(self):
        self.store.put_file(r"C:\a\notes.txt", 1.0, 1, "text",
                            [{"kind": "text", "snippet": "购物前先看日"}, {"kind": "text", "snippet": "写日志"}],
                            np.vstack([vector(0), vector(1)]))
        self.put_doc(r"C:\a\other.txt", 2, "只有日志")

        paths = {self.store.item_path(i) for i in self.store.lexical("日志 购物")}
        self.assertEqual({r"C:\a\notes.txt"}, paths)  # 两个词在不同的块里也算，跨块拼出来的「日写」不算
        self.assertEqual({}, self.store.lexical("日写"))

    def test_common_two_char_words_get_a_smaller_boost(self):
        for i in range(6):
            self.put_doc(rf"C:\a\{i}.txt", i, "查询数据")
        self.put_doc(r"C:\a\数据查询.txt", 7, "没有关键词")

        lex = self.store.lexical("数据 查询")
        by_path = {self.store.item_path(i): s for i, s in lex.items()}
        self.assertEqual(1.0, by_path[r"C:\a\数据查询.txt"])  # 文件名命中照旧
        self.assertAlmostEqual(0.5, by_path[r"C:\a\0.txt"])  # 正文命中 6 个文件：3 / 6

    def test_other_short_terms_only_scan_file_names(self):
        self.put_doc(r"C:\a\税表.txt", 0, "没有关键词")
        self.put_doc(r"C:\a\合同.txt", 1, "这里提到税")

        paths = {self.store.item_path(i) for i in self.store.lexical("税")}
        self.assertEqual({r"C:\a\税表.txt"}, paths)

    def test_keyword_index_follows_replaced_and_deleted_files(self):
        self.put_doc(r"C:\a\x.txt", 0, "alpha keyword 甲乙")
        self.put_doc(r"C:\a\x.txt", 0, "beta keyword 丙丁")
        self.assertEqual({}, self.store.lexical("alpha"))
        self.assertEqual({}, self.store.lexical("甲乙"))
        self.assertEqual(1, len(self.store.lexical("beta")))
        self.assertEqual(1, len(self.store.lexical("丙丁")))

        self.store.clear()
        self.assertEqual({}, self.store.lexical("beta"))
        self.assertEqual({}, self.store.lexical("丙丁"))
        self.assertEqual({}, self.store.lexical("x.txt"))
        self.store.db.execute("INSERT INTO items_fts(items_fts) VALUES('integrity-check')")
        self.store.db.execute("INSERT INTO paths_fts(paths_fts) VALUES('integrity-check')")


class SimilarGroupTests(StoreTestCase):
    def put_image(self, path: str, v: np.ndarray):
        v = (v / np.linalg.norm(v)).astype(np.float32)[None]
        self.store.put_file(path, 1.0, 1, "image", [{"kind": "image", "snippet": path}], v)

    def test_groups_near_identical_images_and_skips_singletons(self):
        rng = np.random.default_rng(0)
        a, b = rng.standard_normal(768), rng.standard_normal(768)
        self.put_image(r"C:\p\a1.jpg", a)
        self.put_image(r"C:\p\a2.jpg", a + 0.05 * rng.standard_normal(768))  # 重新压缩过的副本
        self.put_image(r"C:\p\a3.jpg", a + 0.05 * rng.standard_normal(768))
        self.put_image(r"C:\p\b.jpg", b)
        self.put("doc.txt", 7)  # 文档不参与

        groups = self.store.similar_groups(0.95)

        self.assertEqual([{r"C:\p\a1.jpg", r"C:\p\a2.jpg", r"C:\p\a3.jpg"}], [{h.path for h in g} for g in groups])
        self.assertAlmostEqual(1.0, groups[0][0].score, places=3)
        self.assertEqual([], self.store.similar_groups(0.9999))

    def test_duplicates_are_grouped_by_dhash_distance(self):
        v = np.ones(768)
        for name, h in [("a1", 0x0F0F0F0F0F0F0F0F), ("a2", 0x0F0F0F0F0F0F0F0E),  # 差 1 位：重新压缩过的副本
                        ("a3", 0x0F0F0F0F0F0F0FFF), ("b", 0x7070707070707070),  # a3 差 4 位；b 完全不同
                        ("blank1", 0), ("blank2", 0)]:  # 纯色图不参与
            self.put_image(rf"C:\p\{name}.jpg", v)
            self.store.set_dhash([(self.store.db.execute("SELECT id FROM files WHERE path=?",
                                                          (rf"C:\p\{name}.jpg",)).fetchone()[0], h)])
        self.put_image(r"C:\p\new.jpg", v)  # 还没算 dHash

        groups = self.store.duplicate_groups(max_dist=8)

        self.assertEqual([[r"C:\p\a1.jpg", r"C:\p\a2.jpg", r"C:\p\a3.jpg"]], [[h.path for h in g] for g in groups])
        self.assertAlmostEqual(1 - 1 / 64, groups[0][1].score)
        self.assertEqual([], self.store.duplicate_groups(max_dist=0))
        self.assertEqual([r"C:\p\new.jpg"], [p for _, p in self.store.missing_dhash()])


class CopiesAndHubTests(StoreTestCase):
    def put_image(self, path: str, v: np.ndarray, fp: str | None = None):
        v = (v / np.linalg.norm(v)).astype(np.float32)[None]
        self.store.put_file(path, 1.0, 1, "image", [{"kind": "image", "snippet": None}], v, fp=fp)

    def put_copies(self):
        e = np.eye(768)
        self.put_image(r"C:\a\x.jpg", e[0], fp="h1")
        self.put_image(r"C:\b\x - 副本.jpg", e[0], fp="h1")
        self.put_image(r"C:\c\y.jpg", e[0] + 0.5 * e[1], fp="h2")
        return e[0]

    def test_identical_files_fold_into_one_hit_listing_the_copies(self):
        q = self.put_copies()

        hits = self.store.search(q, k=10)

        self.assertEqual(2, len(hits))
        self.assertEqual({r"C:\a\x.jpg", r"C:\b\x - 副本.jpg"}, {hits[0].path, *hits[0].copies})
        self.assertEqual((r"C:\c\y.jpg", []), (hits[1].path, hits[1].copies))
        self.assertEqual(3, len(self.store.search(q, k=10, dedup_files=False)))

    def test_searching_by_a_file_also_excludes_its_copies(self):
        q = self.put_copies()

        self.assertEqual([r"C:\c\y.jpg"], [h.path for h in self.store.search(q, k=10, exclude_path=r"C:\a\x.jpg")])
        # 查询文件不在索引里（如从桌面拖进来）：由调用方算好指纹
        hits = self.store.search(q, k=10, exclude_path=r"D:\desktop\x.jpg", exclude_hash="h1")
        self.assertEqual([r"C:\c\y.jpg"], [h.path for h in hits])

    def test_hub_images_are_demoted_only_for_images_and_when_asked(self):
        e = np.eye(768)
        self.put_image(r"C:\p\button.png", 0.6 * e[0] + 0.8 * e[1])  # 和通用查询（e0）很像的「万能图」
        self.put_image(r"C:\p\target.png", 0.7 * e[1] + 0.714 * e[2])
        for i in range(3, 6):
            self.put_image(rf"C:\p\{i}.png", e[i])
        self.store.put_file(r"C:\d\doc.txt", 1.0, 1, "text", [{"kind": "text", "snippet": "x"}],
                            (0.6 * e[0] + 0.8 * e[1]).astype(np.float32)[None])
        self.store.set_hub_bank(e[0][None])
        q = e[1]

        with mock.patch.object(config, "HUB_MIN_IMAGES", 3):
            self.assertEqual(r"C:\p\button.png", self.store.search(q, k=1, kinds={"image"})[0].path)
            self.assertEqual(r"C:\p\target.png", self.store.search(q, k=1, kinds={"image"}, hub=0.5)[0].path)
            doc = next(h for h in self.store.search(q, k=10, hub=0.5) if h.kind == "text")
            self.assertAlmostEqual(0.8, doc.score, places=3)  # 文档不扣分
            self.store.set_hub_bank(None)
            self.assertEqual(r"C:\p\button.png", self.store.search(q, k=1, kinds={"image"}, hub=0.5)[0].path)
        self.store.set_hub_bank(e[0][None])
        self.assertEqual(r"C:\p\button.png", self.store.search(q, k=1, kinds={"image"}, hub=0.5)[0].path)  # 图片太少


class VectorStoreMigrationTests(unittest.TestCase):
    def test_opening_a_database_without_keyword_index_builds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.db"
            store = VectorStore(path)
            store.put_file(r"C:\a\report.txt", 1.0, 1, "text", [{"kind": "text", "snippet": "quarterly 季度"}],
                           vector(0))
            store.db.executescript("DROP TABLE paths_fts; DROP TABLE items_fts; DROP TABLE grams_fts;")
            store.db.close()

            store = VectorStore(path)
            self.assertEqual(1, len(store.lexical("quarterly")))
            self.assertEqual(1, len(store.lexical("report")))
            self.assertEqual(1, len(store.lexical("季度")))
            store.db.close()

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
            self.assertEqual([None, None], [r[0] for r in store.db.execute("SELECT dhash FROM files")])
            store.db.close()


if __name__ == "__main__":
    unittest.main()
