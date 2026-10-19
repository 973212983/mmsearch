import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from mmsearch import config, media
from mmsearch.indexer import Indexer
from mmsearch.store import VectorStore
from test_indexer import FakeEmbedder


def write_zip(path: Path, files: dict[str, str]):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)


class OfficeFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pptx_slides_in_order_with_notes(self):
        p = self.dir / "deck.pptx"
        slide = '<p:sld><a:p><a:r><a:t>{}</a:t></a:r></a:p><a:p><a:r><a:t>R&amp;D</a:t></a:r></a:p></p:sld>'
        write_zip(p, {"ppt/slides/slide10.xml": slide.format("第十页"), "ppt/slides/slide2.xml": slide.format("第二页"),
                      "ppt/notesSlides/notesSlide2.xml": "<a:p><a:t>讲稿</a:t></a:p>", "ppt/presentation.xml": "<x/>"})
        text = media.read_text(p)
        self.assertLess(text.index("第二页"), text.index("讲稿"))
        self.assertLess(text.index("讲稿"), text.index("第十页"))
        self.assertIn("R&D", text)

    def test_xlsx_rows_with_shared_and_inline_strings(self):
        m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        p = self.dir / "book.xlsx"
        write_zip(p, {
            "xl/workbook.xml": f'<workbook {m} {r}><sheets><sheet name="预算" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": f'<sst {m}><si><t>项目</t></si><si><r><t>金</t></r><r><t>额</t></r></si></sst>',
            "xl/worksheets/sheet1.xml": f'<worksheet {m}><sheetData>'
                                        '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
                                        '<row><c t="inlineStr"><is><t>差旅</t></is></c><c><v>1200</v></c></row>'
                                        '</sheetData></worksheet>'})
        self.assertEqual("## 预算\n项目\t金额\n差旅\t1200", media.read_text(p))

    def test_epub_follows_spine_order(self):
        p = self.dir / "book.epub"
        write_zip(p, {
            "META-INF/container.xml": '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
            "OEBPS/content.opf": '<package><manifest><item id="c1" href="ch1.xhtml"/><item href="ch2.xhtml" id="c2"/>'
                                 '</manifest><spine><itemref idref="c2"/><itemref idref="c1"/></spine></package>',
            "OEBPS/ch1.xhtml": "<html><head><title>x</title></head><body><p>第一章</p></body></html>",
            "OEBPS/ch2.xhtml": "<html><body><style>p{}</style><h1>序</h1><p>开头</p></body></html>"})
        self.assertEqual("序\n开头\n\n第一章", media.read_text(p))


class ScannedPdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "docs"
        self.root.mkdir()
        self.store = VectorStore(Path(self.tmp.name) / "i.db")
        self.store.add_folder(str(self.root))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def scan_pdf(self, name: str, size=(1240, 1754), pages=2) -> Path:
        imgs = [Image.new("RGB", size, (250, 250, 250 - 40 * i)) for i in range(pages)]
        p = self.root / name
        imgs[0].save(p, save_all=True, append_images=imgs[1:])  # Pillow 写出的 PDF：每页一张整页图片，没有文字层
        return p

    def test_scanned_pages_are_encoded_as_images(self):
        self.scan_pdf("合同扫描.pdf")
        emb = FakeEmbedder()
        Indexer(self.store, emb).run()

        self.assertEqual([("image", 2)], emb.calls)
        rows = self.store.db.execute("SELECT kind, start, snippet FROM items ORDER BY id").fetchall()
        self.assertEqual([("page", 1.0, "第 1 页"), ("page", 2.0, "第 2 页")], rows)

    def test_page_cap_limits_encoded_pages(self):
        self.scan_pdf("长.pdf", pages=4)
        old, config.SCANNED_PDF_MAX_PAGES = config.SCANNED_PDF_MAX_PAGES, 3
        try:
            Indexer(self.store, FakeEmbedder()).run()
        finally:
            config.SCANNED_PDF_MAX_PAGES = old
        self.assertEqual(3, len(self.store))

    def test_unreadable_pdf_is_reported_instead_of_silently_empty(self):
        self.scan_pdf("小图.pdf", size=(300, 300), pages=1)  # 图片太小，不像整页扫描
        Indexer(self.store, FakeEmbedder()).run()

        (path, error), = self.store.failed_files()
        self.assertTrue(path.endswith("小图.pdf"))
        self.assertIn("没有文字层", error)

    def test_old_empty_pdf_records_are_dropped_once(self):
        db = Path(self.tmp.name) / "old.db"
        store = VectorStore(db)
        store.put_file(r"C:\a\scan.pdf", 1.0, 1, "text", [], None)
        store.put_file(r"C:\a\empty.txt", 1.0, 1, "text", [], None)
        store.db.execute("DELETE FROM meta WHERE key='pdf_scan'")
        store.db.commit()
        store.db.close()

        store = VectorStore(db)
        self.assertEqual({r"C:\a\empty.txt"}, set(store.file_states()))
        store.put_file(r"C:\a\scan.pdf", 1.0, 1, "text", [], None)
        store.db.close()
        store = VectorStore(db)
        self.assertIn(r"C:\a\scan.pdf", store.file_states())  # 只迁移一次
        store.db.close()


if __name__ == "__main__":
    unittest.main()
