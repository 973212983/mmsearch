import datetime as dt
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mmsearch import query
from mmsearch.store import VectorStore

TODAY = dt.date(2026, 10, 19)  # 周一
day = lambda y, m, d: dt.datetime(y, m, d).timestamp()


class ParseTests(unittest.TestCase):
    def parse(self, text):
        return query.parse(text, TODAY)

    def test_plain_query_has_no_filters(self):
        text, f = self.parse("海边的日落")
        self.assertEqual("海边的日落", text)
        self.assertFalse(f)

    def test_leading_time_word_with_de(self):
        text, f = self.parse("上周的截图")
        self.assertEqual("截图", text)
        self.assertEqual((day(2026, 10, 12), day(2026, 10, 19)), (f.after, f.before))
        self.assertEqual(["上周"], f.labels)

    def test_time_word_inside_a_sentence_is_kept(self):
        text, f = self.parse("去年在海边拍的照片")  # 说的是拍摄时间，不是修改时间
        self.assertEqual("去年在海边拍的照片", text)
        self.assertFalse(f)

    def test_standalone_time_words(self):
        _, f = self.parse("会议 昨天")
        self.assertEqual((day(2026, 10, 18), day(2026, 10, 19)), (f.after, f.before))
        _, f = self.parse("报告 上个月")
        self.assertEqual((day(2026, 9, 1), day(2026, 10, 1)), (f.after, f.before))
        _, f = self.parse("最近7天 截图")
        self.assertEqual((day(2026, 10, 13), day(2026, 10, 20)), (f.after, f.before))
        _, f = self.parse("近两周 截图")
        self.assertEqual(day(2026, 10, 6), f.after)

    def test_operators(self):
        text, f = self.parse("报销 ext:pdf,DOCX in:财务 after:2026-10-01 before:2026/10/15")
        self.assertEqual("报销", text)
        self.assertEqual({".pdf", ".docx"}, f.exts)
        self.assertEqual(["财务"], f.dirs)
        self.assertEqual((day(2026, 10, 1), day(2026, 10, 16)), (f.after, f.before))

    def test_dot_extension_and_unknown_operators(self):
        text, f = self.parse("合同 .pdf foo:bar")
        self.assertEqual("合同 foo:bar", text)
        self.assertEqual({".pdf"}, f.exts)

    def test_only_filters(self):
        text, f = self.parse("今天 ext:png")
        self.assertEqual("", text)
        self.assertEqual({".png"}, f.exts)


class StoreFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VectorStore(Path(self.tmp.name) / "i.db")
        for i, (path, mtime) in enumerate([(r"C:\财务\2026\报销.pdf", day(2026, 10, 14)),
                                           (r"C:\财务\旧\报销.docx", day(2025, 3, 1)),
                                           (r"C:\图片\截图.png", day(2026, 10, 18))]):
            v = np.zeros((1, 768), np.float32)
            v[0, i] = 1
            kind = "image" if path.endswith(".png") else "text"
            self.store.put_file(path, mtime, 1, kind, [{"kind": kind, "snippet": "x"}], v)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def files(self, text):
        return self.store.filter_files(query.parse(text, TODAY)[1])

    def paths(self, hits):
        return [Path(h.path).name for h in hits]

    def test_filter_files_by_ext_folder_and_time(self):
        self.assertEqual(1, len(self.files("ext:pdf")))
        self.assertEqual(2, len(self.files("in:财务")))
        self.assertEqual(2, len(self.files("最近7天")))
        self.assertEqual(0, len(self.files("in:图片 ext:pdf")))

    def test_search_only_returns_allowed_files(self):
        q = np.ones(768, np.float32) / np.sqrt(768)
        hits = self.store.search(q, k=10, files=self.files("in:财务"))
        self.assertEqual({"报销.pdf", "报销.docx"}, set(self.paths(hits)))

    def test_browse_lists_newest_first_and_respects_kinds(self):
        self.assertEqual(["截图.png", "报销.pdf", "报销.docx"], self.paths(self.store.browse(self.files("ext:pdf,docx,png"))))
        self.assertEqual(["截图.png"], self.paths(self.store.browse(self.files("ext:pdf,docx,png"), kinds={"image"})))


if __name__ == "__main__":
    unittest.main()
