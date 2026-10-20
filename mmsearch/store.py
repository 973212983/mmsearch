"""向量存储：SQLite 持久化 + 内存 numpy 矩阵做两阶段检索。

- 每个 item（文本块 / 图片 / 音视频片段）存一条 768d float16 向量
- 内存中维护 256d 归一化 float32 矩阵做粗排（Matryoshka 前缀），候选再用 768d 精排
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    modality TEXT NOT NULL,
    error TEXT,
    hash TEXT,                   -- 内容指纹：移动/改名/复制过的文件直接复用向量
    dhash INTEGER                -- 图片的差值哈希（media.dhash），找几乎相同的图；NULL = 还没算，0 = 纯色图
);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,          -- text / image / audio / video / video_audio
    start REAL, end REAL,        -- 音视频片段时间（秒）
    snippet TEXT,                -- 文本块内容或描述
    vec BLOB NOT NULL            -- 768 x float16
);
CREATE INDEX IF NOT EXISTS idx_items_file ON items(file_id);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);  -- 如 model：建索引所用模型的指纹
CREATE INDEX IF NOT EXISTS idx_files_error ON files(path) WHERE error IS NOT NULL;
"""

# 关键词索引（FTS5 trigram：中英文都按任意子串匹配，不需要分词）。向量检索对文件名、代码标识符、
# 单号、精确短语不敏感，这些靠关键词命中给结果加分。两张都是外部内容表（不重复存正文），由触发器保持同步。
# paths_fts：文件路径（rowid = files.id）；items_fts：文本块（rowid = items.id）
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS paths_fts USING fts5(path, content='files', content_rowid='id', tokenize='trigram');
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(snippet, content='items', content_rowid='id',
                                                        tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS files_fts_ai AFTER INSERT ON files BEGIN
    INSERT INTO paths_fts(rowid, path) VALUES (new.id, new.path);
END;
CREATE TRIGGER IF NOT EXISTS files_fts_ad AFTER DELETE ON files BEGIN
    INSERT INTO paths_fts(paths_fts, rowid, path) VALUES ('delete', old.id, old.path);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, snippet) VALUES (new.id, new.snippet);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, snippet) VALUES ('delete', old.id, old.snippet);
END;
"""

# 两个字的中文词 trigram 索引不了：每个文件把正文里的所有中文二字组（去重、空格分隔）存一行（rowid = files.id），
# 按文件查「这几个词都出现过」——人记得的往往是同一份文件里不在一处的几个词。只存索引不存原文。
GRAMS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS grams_fts USING fts5(grams, content='', contentless_delete=1);
CREATE TRIGGER IF NOT EXISTS files_grams_ad AFTER DELETE ON files BEGIN
    DELETE FROM grams_fts WHERE rowid = old.id;
END;
"""
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")


def _bigrams(texts) -> str:
    grams = set()
    for t in texts:
        for run in _CJK.findall(t or ""):
            grams.update(run[i:i + 2] for i in range(len(run) - 1))
    return " ".join(grams)


@dataclass
class Hit:
    item_id: int
    score: float
    path: str
    kind: str
    start: float | None
    end: float | None
    snippet: str | None
    mtime: float | None = None
    lexical: bool = False  # 关键词命中（文件名或正文），score 里含 LEXICAL_BOOST 加分
    copies: list[str] = field(default_factory=list)  # 内容相同（指纹一致）的其他文件，合并在这一条里


def _normalize(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.maximum(n, 1e-12)


class VectorStore:
    def __init__(self, db_path: Path = config.DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # WAL 下安全，每个文件一次提交不必各自 fsync
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        if "hash" not in cols:
            self.db.execute("ALTER TABLE files ADD COLUMN hash TEXT")  # 旧版索引库：之前的记录没有指纹
        if "dhash" not in cols:
            self.db.execute("ALTER TABLE files ADD COLUMN dhash INTEGER")  # 用到时由 missing_dhash/set_dhash 补算
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash) WHERE hash IS NOT NULL")
        new_fts = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='paths_fts'").fetchone()
        self.db.executescript(FTS_SCHEMA)
        if new_fts:  # 旧版索引库：给已有记录补上关键词索引
            self.db.execute("INSERT INTO paths_fts(paths_fts) VALUES ('rebuild')")
            self.db.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
        new_grams = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='grams_fts'").fetchone()
        self.db.executescript(GRAMS_SCHEMA)
        if new_grams:  # 旧版索引库：给已有的文本补上二字组索引
            fid, snippets = None, []
            for f, s in self.db.execute("SELECT file_id, snippet FROM items WHERE kind='text' ORDER BY file_id"):
                if f != fid:
                    self._put_grams(fid, snippets)
                    fid, snippets = f, []
                snippets.append(s)
            self._put_grams(fid, snippets)
        if not self.db.execute("SELECT 1 FROM meta WHERE key='pdf_scan'").fetchone():
            # 旧版把读不出文字的 PDF 记成「成功、0 条」，mtime 没变就不会再处理：删掉记录，下次扫描按扫描件重新处理
            self.db.execute("DELETE FROM files WHERE path LIKE '%.pdf' AND error IS NULL "
                            "AND NOT EXISTS (SELECT 1 FROM items WHERE items.file_id = files.id)")
            self.db.execute("INSERT INTO meta(key, value) VALUES ('pdf_scan', '1')")
        self.db.commit()
        self.lock = threading.RLock()
        self._hub_bank: np.ndarray | None = None  # hubs.bank_vectors()，模型就绪后设置
        self._hub_center: tuple[float, int] | None = None  # (图片万能分的中位数, 算的时候有多少张图)
        self._load()

    # ---------- 内存索引 ----------
    def _load(self):
        rows = self.db.execute("SELECT id, kind, vec, file_id FROM items").fetchall()
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        kinds = np.array([r[1] for r in rows], dtype=object)
        fids = np.array([r[3] for r in rows], dtype=np.int64)
        if rows:
            full = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float16)
            full = full.reshape(len(rows), config.FULL_DIM).copy()  # frombuffer 是只读的，_remove_mem 要原地填洞
        else:
            full = np.zeros((0, config.FULL_DIM), dtype=np.float16)
        with self.lock:
            self._set_mem(ids, kinds, full, _normalize(full[:, : config.COARSE_DIM].astype(np.float32)), fids)

    # 底层缓冲区按容量翻倍预分配，前 _n 行有效，ids/kinds/full/coarse/fids 是前 _n 行的视图。
    # 追加只写空闲行，不再每个文件都复制整个矩阵（否则文件越多索引越慢，总耗时平方增长）。
    # 已发布的视图看不到之后追加的行；扩容换新缓冲区，旧视图仍有效，搜索拿到的快照始终一致。
    # fids：每条所属的文件 id，过滤条件（时间、类型、文件夹）按文件算出后直接在内存里做掩码。
    def _set_mem(self, ids, kinds, full, coarse, fids):
        self._buf = [ids, kinds, full, coarse, fids]
        self._n = len(ids)
        self._n_image = int(np.count_nonzero(kinds == "image"))
        self._positions = {int(item_id): pos for pos, item_id in enumerate(ids)}
        self._publish()

    def _publish(self):
        n = self._n
        self.ids, self.kinds, self.full, self.coarse, self.fids = (b[:n] for b in self._buf)

    def _append_mem(self, ids, kinds, vecs16, fid: int):
        with self.lock:
            n, m = self._n, len(ids)
            if n + m > len(self._buf[0]):
                cap = max(1024, 2 * (n + m))
                grown = []
                for b in self._buf:
                    g = np.empty((cap,) + b.shape[1:], dtype=b.dtype)
                    g[:n] = b[:n]
                    grown.append(g)
                self._buf = grown
            b_ids, b_kinds, b_full, b_coarse, b_fids = self._buf
            b_ids[n:n + m] = ids
            b_kinds[n:n + m] = kinds
            b_full[n:n + m] = vecs16
            b_coarse[n:n + m] = _normalize(vecs16[:, : config.COARSE_DIM].astype(np.float32))
            b_fids[n:n + m] = fid
            for offset, item_id in enumerate(ids):
                self._positions[int(item_id)] = n + offset
            self._n = n + m
            self._n_image += sum(k == "image" for k in kinds)
            self._publish()

    def _remove_mem(self, item_ids):
        if not len(item_ids):
            return
        with self.lock:
            # 向量顺序没有语义：用末行填洞即可。旧实现每删一个文件都复制整份矩阵，
            # 批量更新已有文件时会退化成 O(文件数 x 索引大小)。槽位回收只搬动被删行数量的数据。
            for item_id in item_ids:
                pos = self._positions.pop(int(item_id), None)
                if pos is None:
                    continue
                self._n_image -= self._buf[1][pos] == "image"
                last = self._n - 1
                if pos != last:
                    moved_id = int(self._buf[0][last])
                    for buf in self._buf:
                        buf[pos] = buf[last]
                    self._positions[moved_id] = pos
                self._n = last
            self._publish()

    def __len__(self):
        return self._n

    # ---------- 文件夹 ----------
    def folders(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT path FROM folders ORDER BY path")]

    def add_folder(self, path: str):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO folders(path) VALUES(?)", (path,))
            self.db.commit()

    def remove_folder(self, path: str):
        """移除文件夹及其下所有已索引文件。"""
        lo, hi = self._range(path)
        with self.lock:
            self.db.execute("DELETE FROM folders WHERE path=?", (path,))
            fids = [r[0] for r in self.db.execute(
                "SELECT id FROM files WHERE path=? OR (path>=? AND path<?)", (path, lo, hi))]
            self.delete_files(fids)

    # ---------- 文件 ----------
    def file_states(self) -> dict[str, tuple[int, float, int]]:
        with self.lock:
            return {p: (i, m, s) for i, p, m, s in self.db.execute("SELECT id, path, mtime, size FROM files")}

    def _item_ids(self, file_ids, delete=False) -> list[int]:
        item_ids = []
        for s in range(0, len(file_ids), 500):  # SQLite 参数个数有上限
            part = list(file_ids[s: s + 500])
            q = ",".join("?" * len(part))
            item_ids += [r[0] for r in self.db.execute(f"SELECT id FROM items WHERE file_id IN ({q})", part)]
            if delete:
                self.db.execute(f"DELETE FROM files WHERE id IN ({q})", part)
        return item_ids

    def delete_files(self, file_ids):
        if not file_ids:
            return
        with self.lock:
            item_ids = self._item_ids(file_ids, delete=True)
            self.db.commit()
            self._remove_mem(item_ids)

    def hide_files(self, file_ids):
        """只从内存索引里拿掉（搜索不再命中），数据库记录留着，之后由 delete_files 删除。
        索引一轮开始时用于已消失的文件：本轮里被移动/改名到新路径的文件还能复用它们的向量。"""
        if file_ids:
            with self.lock:
                self._remove_mem(self._item_ids(file_ids))

    def reuse(self, path: str, mtime: float, size: int, modality: str, fp: str, name: str | None = None) -> bool:
        """索引里已有内容相同（指纹一致、没有出错）的文件就复用它的向量，不必重新编码，返回 True。
        同一路径（只是 mtime 变了）只更新 mtime；其他路径（移动、改名、复制）复制它的全部 item。
        name：文本的向量里带着文件名（title），只复用同名文件的。"""
        with self.lock:
            rows = self.db.execute("SELECT id, path, dhash FROM files WHERE hash=? AND modality=? AND error IS NULL",
                                   (fp, modality)).fetchall()
            same = [r for r in rows if r[1] == path]
            if same:
                self.db.execute("UPDATE files SET mtime=?, size=? WHERE id=?", (mtime, size, same[0][0]))
                self.db.commit()
                return True
            src = next((r for r in rows if name is None or os.path.basename(r[1]) == name), None)
            if src is None:
                return False
            got = self.db.execute("SELECT kind, start, end, snippet, vec FROM items WHERE file_id=? ORDER BY id",
                                  (src[0],)).fetchall()
            items = [{"kind": k, "start": s, "end": e, "snippet": sn} for k, s, e, sn, _ in got]
            vecs = np.frombuffer(b"".join(r[4] for r in got), dtype=np.float16).reshape(len(got), config.FULL_DIM)
            self.put_file(path, mtime, size, modality, items, vecs, fp=fp, dhash=src[2])
            return True

    def put_file(self, path: str, mtime: float, size: int, modality: str, items: list[dict],
                 vecs: np.ndarray | None, error: str | None = None, fp: str | None = None,
                 dhash: int | None = None):
        """写入（或替换）一个文件的全部 item。vecs: (len(items), 768) 已归一化。fp：内容指纹。dhash：图片的差值哈希。"""
        with self.lock:
            old = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            old_item_ids = []
            if old:
                old_item_ids = [r[0] for r in self.db.execute(
                    "SELECT id FROM items WHERE file_id=?", (old[0],))]
            vecs16 = np.ascontiguousarray(vecs, dtype=np.float16) if items else None
            try:
                if old:
                    self.db.execute("DELETE FROM files WHERE id=?", (old[0],))
                cur = self.db.execute(
                    "INSERT INTO files(path, mtime, size, modality, error, hash, dhash) VALUES(?,?,?,?,?,?,?)",
                    (path, mtime, size, modality, error, fp, dhash))
                fid = cur.lastrowid
                if items:
                    self.db.executemany(
                        "INSERT INTO items(file_id, kind, start, end, snippet, vec) VALUES(?,?,?,?,?,?)",
                        [(fid, it["kind"], it.get("start"), it.get("end"), it.get("snippet"), v.tobytes())
                         for it, v in zip(items, vecs16)])
                    self._put_grams(fid, [it.get("snippet") for it in items if it["kind"] == "text"])
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            self._remove_mem(old_item_ids)
            if items:
                new_ids = np.fromiter(
                    (r[0] for r in self.db.execute("SELECT id FROM items WHERE file_id=? ORDER BY id", (fid,))),
                    dtype=np.int64, count=len(items))
                self._append_mem(new_ids, [it["kind"] for it in items], vecs16, fid)

    def _put_grams(self, fid: int | None, snippets: list[str]):
        grams = _bigrams(snippets) if fid is not None else ""
        if grams:
            self.db.execute("INSERT INTO grams_fts(rowid, grams) VALUES (?, ?)", (fid, grams))

    def stats(self) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT modality, COUNT(*) FROM files GROUP BY modality").fetchall()
            errors = self.db.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
        return {"files": dict(rows), "items": len(self), "errors": errors}

    def vector(self, item_id: int) -> np.ndarray | None:
        """取已索引条目的 768d 向量（用于“找相似”，无需重新编码）。"""
        with self.lock:
            pos = np.flatnonzero(self.ids == item_id)
            return self.full[pos[0]].astype(np.float32) if len(pos) else None

    def item_path(self, item_id: int) -> str | None:
        with self.lock:
            r = self.db.execute("SELECT files.path FROM items JOIN files ON files.id=items.file_id "
                                "WHERE items.id=?", (item_id,)).fetchone()
        return r[0] if r else None

    @staticmethod
    def _range(folder: str) -> tuple[str, str]:
        """folder 下的路径都落在 [lo, hi) 内（"]" 紧跟在 "\\" 之后），能用上 path 的唯一索引，不必全表扫描。"""
        lo = folder.rstrip("\\/") + "\\"
        return lo, lo[:-1] + "]"

    def folder_counts(self, folder: str) -> dict[str, int]:
        lo, hi = self._range(folder)
        with self.lock:
            rows = self.db.execute(
                "SELECT modality, COUNT(*) FROM files WHERE path>=? AND path<? GROUP BY modality",
                (lo, hi)).fetchall()
        return dict(rows)

    def failed_files(self) -> list[tuple[str, str]]:
        with self.lock:
            return self.db.execute(
                "SELECT path, error FROM files WHERE error IS NOT NULL ORDER BY path").fetchall()

    def retry_failed(self):
        """删除失败记录，下次索引时重新处理。"""
        with self.lock:
            self.db.execute("DELETE FROM files WHERE error IS NOT NULL")
            self.db.commit()

    def get_meta(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
            self.db.commit()

    def clear(self, modalities: set[str] | None = None):
        """清空索引（保留文件夹列表）。modalities 为空表示全部。"""
        with self.lock:
            if modalities:
                q = ",".join("?" * len(modalities))
                fids = [r[0] for r in self.db.execute(f"SELECT id FROM files WHERE modality IN ({q})",
                                                      list(modalities))]
                self.delete_files(fids)
            else:
                self.db.execute("DELETE FROM files")
                self.db.commit()
                self._load()

    # ---------- 检索 ----------
    def search(self, q: np.ndarray, k: int = 50, kinds: set[str] | None = None,
               dedup_files: bool = True, exclude_path: str | None = None,
               lex: dict[int, float] | None = None, boost: float = config.LEXICAL_BOOST,
               files: np.ndarray | None = None, exclude_hash: str | None = None, hub: float = 0.0) -> list[Hit]:
        """两阶段检索：256d 粗排 → 768d 精排。q: (768,) 已归一化。
        exclude_path：排除查询文件自身（找相似），内容和它相同的副本也排除；查询文件不在索引里时由
        exclude_hash 给出它的指纹。dedup_files 时内容相同的文件合并成一条（Hit.copies）。
        lex：lexical(文字查询) 的结果，命中的条目（不在向量候选里的也补进来）得分加 boost × 命中强度。
        各类型标签共用同一个查询，lex 只需算一次。files：filter_files() 的结果，只在这些文件里找。
        hub：图片得分减去 hub × 万能分（见 hubs.py），只用于含文字的查询。"""
        q = np.asarray(q, dtype=np.float32).reshape(-1)
        lex = lex if boost > 0 else None
        with self.lock:
            ids, item_kinds, full, coarse = self.ids, self.kinds, self.full, self.coarse
            if not len(ids):
                return []
            mask = self._mask(kinds, files)
            qc = q[: config.COARSE_DIM]
            qc = qc / max(np.linalg.norm(qc), 1e-12)
            scores = coarse @ qc
            if mask is not None:
                scores = np.where(mask, scores, -np.inf)
            n_cand = min(len(scores), max(config.RERANK_CANDIDATES, k * 4))
            cand = np.argpartition(-scores, n_cand - 1)[:n_cand]
            cand = cand[np.isfinite(scores[cand])]
            bonus = np.zeros(len(cand), np.float32)
            if lex:
                pos = np.array([p for p in map(self._positions.get, lex) if p is not None and p < len(ids)], np.int64)
                if mask is not None:
                    pos = pos[mask[pos]]
                cand = np.union1d(cand, pos)
                bonus = boost * np.array([lex.get(int(i), 0.0) for i in ids[cand]], np.float32)
            cand_full = full[cand].astype(np.float32)
            fine = cand_full @ q + bonus
            center = self._hub_median() if hub > 0 else None
            if center is not None:
                img = item_kinds[cand] == "image"
                fine[img] -= hub * ((cand_full[img] @ self._hub_bank.T).mean(1) - center)
            fine_order = np.argsort(-fine)
            ordered_ids = ids[cand[fine_order]].copy()
            ordered_scores = fine[fine_order].copy()
            ordered_lex = bonus[fine_order] > 0
        return self._hits(ordered_ids, ordered_scores, k, dedup_files, exclude_path, ordered_lex,
                          fold=dedup_files, exclude_hash=exclude_hash)

    def set_hub_bank(self, bank: np.ndarray | None):
        """hubs.bank_vectors() 的结果；None 表示不做万能图修正。"""
        with self.lock:
            self._hub_bank = None if bank is None else np.asarray(bank, np.float32)
            self._hub_center = None

    def _hub_median(self) -> float | None:
        """所有图片万能分的中位数（抽样）。图片数量变化超过一半时重算；图片太少时不修正，返回 None。"""
        n = self._n_image
        if self._hub_bank is None or n < config.HUB_MIN_IMAGES:
            return None
        if self._hub_center is None or not (self._hub_center[1] / 1.5 <= n <= self._hub_center[1] * 1.5):
            pos = np.flatnonzero(self.kinds == "image")
            if len(pos) > 20000:
                pos = np.random.default_rng(0).choice(pos, 20000, replace=False)
            raw = (self.full[pos].astype(np.float32) @ self._hub_bank.T).mean(1)
            self._hub_center = (float(np.median(raw)), n)
        return self._hub_center[0]

    def similar_groups(self, threshold: float, kinds: set[str] | None = None, limit: int = 300) -> list[list[Hit]]:
        """相似的图片分组：图片向量两两余弦 ≥ threshold 的连成一组，每组按文件去重。
        score 是和组里第一张的相似度。分块矩阵乘法，一万张图也只要零点几秒。"""
        kinds = kinds or {"image"}
        with self.lock:
            pos = np.flatnonzero(self._mask(kinds, None))
            ids, fids = self.ids[pos].copy(), self.fids[pos].copy()
            vecs = _normalize(self.full[pos].astype(np.float32))
        sim = lambda s: vecs[s: s + 1024] @ vecs.T
        return self._groups(ids, fids, sim, threshold, lambda g: vecs[g] @ vecs[g[0]], limit)

    def duplicate_groups(self, max_dist: int = config.DHASH_SAME_MAX, limit: int = 300) -> list[list[Hit]]:
        """几乎相同的图片分组（副本、缩放、重新压缩）：dHash 汉明距离 ≤ max_dist 的连成一组。
        score = 1 - 和组里第一张的距离 / 64。还没算 dHash 的图（missing_dhash）不参与。"""
        with self.lock:
            rows = self.db.execute(
                "SELECT items.id, files.id, files.dhash FROM files JOIN items ON items.file_id=files.id "
                "WHERE files.modality='image' AND items.kind='image' AND files.error IS NULL "
                "AND files.dhash IS NOT NULL AND files.dhash != 0").fetchall()
            rows = [r for r in rows if r[0] in self._positions]  # 已从内存索引拿掉（hide_files）的不算
        ids = np.array([r[0] for r in rows], np.int64)
        fids = np.array([r[1] for r in rows], np.int64)
        h = np.array([r[2] for r in rows], np.int64).view(np.uint64)
        same = lambda s: 1 - np.bitwise_count(h[s: s + 1024, None] ^ h[None]) / 64
        return self._groups(ids, fids, same, 1 - max_dist / 64, lambda g: 1 - np.bitwise_count(h[g] ^ h[g[0]]) / 64,
                            limit)

    def _groups(self, ids, fids, block_sim, threshold, sims_to_first, limit) -> list[list[Hit]]:
        """两两相似度 ≥ threshold 的连成一组（并查集），组按大小、再按组内最高相似度排序。
        block_sim(s)：第 s 行起 1024 行和全部的相似度矩阵；sims_to_first(g)：组里每张和第一张的相似度。"""
        n = len(ids)
        parent = np.arange(n)

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        best = np.zeros(n, np.float32)
        for s in range(0, n, 1024):
            sim = block_sim(s)
            ii, jj = np.nonzero(sim >= threshold)
            keep = jj > ii + s  # 每对只算一次，也不和自己比
            for i, j in zip(ii[keep] + s, jj[keep]):
                if fids[i] == fids[j]:
                    continue
                best[i] = max(best[i], sim[i - s, j])
                best[j] = max(best[j], sim[i - s, j])
                a, b = find(i), find(j)
                if a != b:
                    parent[b] = a
        groups: dict[int, list[int]] = {}
        for i in np.flatnonzero(best > 0):
            groups.setdefault(find(i), []).append(int(i))
        ordered = sorted(groups.values(), key=lambda g: (-len(g), -float(best[g].max())))[:limit]
        out = []
        for g in ordered:
            sims = sims_to_first(g)
            order = np.argsort(-sims, kind="stable")
            out.append(self._hits(ids[np.array(g)[order]], sims[order], len(g), True))
        return [g for g in out if len(g) > 1]

    def missing_dhash(self) -> list[tuple[int, str]]:
        """还没算 dHash 的图片 (file id, 路径)：旧版索引、复用来的记录。"""
        with self.lock:
            return self.db.execute("SELECT id, path FROM files WHERE modality='image' AND error IS NULL "
                                   "AND dhash IS NULL").fetchall()

    def set_dhash(self, rows: list[tuple[int, int]]):
        """rows: [(file id, dhash)]"""
        with self.lock:
            self.db.executemany("UPDATE files SET dhash=? WHERE id=?", [(h, fid) for fid, h in rows])
            self.db.commit()

    def _mask(self, kinds: set[str] | None, files: np.ndarray | None) -> np.ndarray | None:
        """按类型和文件过滤的掩码（调用方持有锁）；都不限时返回 None。"""
        mask = np.isin(self.kinds, list(kinds)) if kinds else None
        if files is not None:
            in_files = np.isin(self.fids, files)
            mask = in_files if mask is None else mask & in_files
        return mask

    def filter_files(self, f) -> np.ndarray:
        """符合过滤条件（query.Filters：时间、扩展名、文件夹）的文件 id，按修改时间从新到旧。"""
        where, args = ["error IS NULL"], []
        if f.after is not None:
            where.append("mtime >= ?")
            args.append(f.after)
        if f.before is not None:
            where.append("mtime < ?")
            args.append(f.before)
        if f.exts:
            where.append("(" + " OR ".join("path LIKE ?" for _ in f.exts) + ")")
            args += ["%" + e for e in sorted(f.exts)]
        with self.lock:
            rows = self.db.execute(f"SELECT id, path FROM files WHERE {' AND '.join(where)} ORDER BY mtime DESC",
                                   args).fetchall()
        if f.dirs:
            rows = [r for r in rows if all(d in os.path.dirname(r[1]).lower() for d in f.dirs)]
        return np.array([r[0] for r in rows], dtype=np.int64)

    def browse(self, files: np.ndarray, k: int = 50, kinds: set[str] | None = None) -> list[Hit]:
        """只有过滤条件、没有查询内容时（如「上周 ext:png」）：按修改时间从新到旧列出文件，每个文件取第一条。"""
        with self.lock:
            mask = self._mask(kinds, files)
            pos = np.flatnonzero(mask) if mask is not None else np.arange(len(self.ids))
            fids, first = np.unique(self.fids[pos], return_index=True)
            item_of = dict(zip(fids.tolist(), self.ids[pos[first]].tolist()))
        order = [item_of[f] for f in files.tolist() if f in item_of]
        return self._hits(np.array(order, np.int64), np.zeros(len(order), np.float32), k, True)

    def lexical(self, text: str) -> dict[int, float]:
        """关键词命中：{item_id: 强度 0~1}。查询按空白拆词，所有词都要出现（AND）。
        - 正文：按 bm25 相对最佳结果的比例给强度。
        - 文件：所有词都出现在路径里、且至少一个词在文件名里（「项目A 报价」这样带上文件夹也能找到）。
          强度看查询词占文件名主干的比例（搜的就是这个文件名时为 1），以及命中的文件少不少（单号、编号这类
          只有一两个文件含有的词也算精确命中），取较大者。
        - 两个字的中文词：trigram 只能索引 3 个字符以上的词，这类词在正文里按文件查（grams_fts）：
          所有两字词都在这个文件里出现过（有长词时长词也要出现），强度看命中的文件少不少，
          常见词组合命中的文件多、加分就小。其他短词（单个汉字、两个字母）只在路径上过滤。
        全是短词时路径只做子串扫描（路径表小，扫描很快）。"""
        # 单个英文字母/数字几乎每个路径都有，不算关键词
        terms = list(dict.fromkeys(t.lower() for t in text.split() if len(t) > 1 or not t.isascii()))
        long_terms = [t for t in terms if len(t) >= 3]
        if not terms:
            return {}
        n = config.LEXICAL_CANDIDATES
        out: dict[int, float] = {}
        quote = lambda ts: " AND ".join('"' + t.replace('"', '""') + '"' for t in ts)
        with self.lock:
            if long_terms:
                match = quote(long_terms)
                paths = self.db.execute("SELECT rowid, path FROM paths_fts WHERE paths_fts MATCH ? LIMIT ?",
                                        (match, n * 4)).fetchall()
                rows = self.db.execute("SELECT rowid, bm25(items_fts) FROM items_fts WHERE items_fts MATCH ? "
                                       "ORDER BY rank LIMIT ?", (match, n)).fetchall()
                if rows:
                    best = rows[0][1] or -1.0  # bm25 越小越好（负数）
                    for iid, s in rows:
                        out[iid] = max(0.05, min(1.0, s / best)) if best < 0 else 1.0
            else:
                like = " AND ".join("instr(lower(path), ?) > 0" for _ in terms)
                paths = self.db.execute(f"SELECT id, path FROM files WHERE {like} LIMIT ?", (*terms, n * 4)).fetchall()
            files = {}
            for fid, path in paths:
                path = path.lower()
                name = os.path.basename(path)
                in_name = [t for t in terms if t in name]
                if in_name and all(t in path for t in terms):
                    files[fid] = sum(map(len, in_name)) / max(len(os.path.splitext(name)[0]), 1)
            strength = {}
            if files:
                specific = 3 / len(files)
                strength = {fid: min(1.0, max(s, specific, 0.2)) for fid, s in files.items()}
            pairs = [t for t in terms if len(t) == 2 and _CJK.fullmatch(t)]
            if pairs:
                found = {r[0] for r in self.db.execute("SELECT rowid FROM grams_fts WHERE grams_fts MATCH ?",
                                                       (quote(pairs),))}
                if found and long_terms:
                    found &= {r[0] for r in self.db.execute(
                        "SELECT DISTINCT file_id FROM items WHERE id IN "
                        "(SELECT rowid FROM items_fts WHERE items_fts MATCH ?)", (match,))}
                if 0 < len(found) <= n * 4:
                    for fid in found:
                        strength[fid] = max(strength.get(fid, 0.0), min(1.0, 3 / len(found)))
            if strength:
                fids = list(strength)
                q = ",".join("?" * len(fids))
                for iid, fid in self.db.execute(f"SELECT id, file_id FROM items WHERE file_id IN ({q})", fids):
                    out[iid] = max(out.get(iid, 0.0), strength[fid])
        return out

    def _hits(self, item_ids, scores, k, dedup_files, exclude_path=None, lexical=None,
              fold=False, exclude_hash=None) -> list[Hit]:
        """fold：内容相同（指纹一致）的文件只保留排在最前的一条，其余路径记在 Hit.copies。
        exclude_hash：和查询文件内容相同的也排除（没给时按 exclude_path 在索引里查）。"""
        out, seen = [], set()
        id_list = [int(i) for i in item_ids]
        if not id_list:
            return out
        q = ",".join("?" * len(id_list))
        skip = os.path.normcase(os.path.abspath(exclude_path)) if exclude_path else None
        with self.lock:
            rows = {r[0]: r for r in self.db.execute(
                f"SELECT items.id, files.path, items.kind, items.start, items.end, items.snippet, files.id, files.mtime, "
                f"files.hash FROM items JOIN files ON files.id=items.file_id WHERE items.id IN ({q})", id_list)}
            if skip and exclude_hash is None:
                r = self.db.execute("SELECT hash FROM files WHERE path=?", (os.path.abspath(exclude_path),)).fetchone()
                exclude_hash = r and r[0]
        by_hash: dict[str, Hit] = {}
        for i, (iid, s) in enumerate(zip(id_list, scores)):
            r = rows.get(iid)
            if r is None:
                continue
            if dedup_files and r[6] in seen:
                continue
            seen.add(r[6])
            if skip and (os.path.normcase(r[1]) == skip or (exclude_hash and r[8] == exclude_hash)):
                continue
            if fold and r[8] in by_hash:
                continue
            out.append(Hit(iid, float(s), r[1], r[2], r[3], r[4], r[5], r[7], bool(lexical is not None and lexical[i])))
            if fold and r[8]:
                by_hash[r[8]] = out[-1]
            if len(out) >= k:
                break
        if by_hash:  # 副本列全（不只是排进候选的那些）
            hs = list(by_hash)
            with self.lock:
                for s in range(0, len(hs), 500):
                    part = hs[s: s + 500]
                    for fp, path in self.db.execute(
                            f"SELECT hash, path FROM files WHERE hash IN ({','.join('?' * len(part))}) "
                            f"AND error IS NULL ORDER BY path", part):
                        if path != by_hash[fp].path:
                            by_hash[fp].copies.append(path)
        return out
