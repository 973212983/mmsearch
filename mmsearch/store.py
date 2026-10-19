"""向量存储：SQLite 持久化 + 内存 numpy 矩阵做两阶段检索。

- 每个 item（文本块 / 图片 / 音视频片段）存一条 768d float16 向量
- 内存中维护 256d 归一化 float32 矩阵做粗排（Matryoshka 前缀），候选再用 768d 精排
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
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
    hash TEXT                    -- 内容指纹：移动/改名/复制过的文件直接复用向量
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
        if "hash" not in {r[1] for r in self.db.execute("PRAGMA table_info(files)")}:
            self.db.execute("ALTER TABLE files ADD COLUMN hash TEXT")  # 旧版索引库：之前的记录没有指纹
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash) WHERE hash IS NOT NULL")
        new_fts = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='paths_fts'").fetchone()
        self.db.executescript(FTS_SCHEMA)
        if new_fts:  # 旧版索引库：给已有记录补上关键词索引
            self.db.execute("INSERT INTO paths_fts(paths_fts) VALUES ('rebuild')")
            self.db.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
        self.db.commit()
        self.lock = threading.RLock()
        self._load()

    # ---------- 内存索引 ----------
    def _load(self):
        rows = self.db.execute("SELECT id, kind, vec FROM items").fetchall()
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        kinds = np.array([r[1] for r in rows], dtype=object)
        if rows:
            full = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float16)
            full = full.reshape(len(rows), config.FULL_DIM).copy()  # frombuffer 是只读的，_remove_mem 要原地填洞
        else:
            full = np.zeros((0, config.FULL_DIM), dtype=np.float16)
        with self.lock:
            self._set_mem(ids, kinds, full, _normalize(full[:, : config.COARSE_DIM].astype(np.float32)))

    # 底层缓冲区按容量翻倍预分配，前 _n 行有效，ids/kinds/full/coarse 是前 _n 行的视图。
    # 追加只写空闲行，不再每个文件都复制整个矩阵（否则文件越多索引越慢，总耗时平方增长）。
    # 已发布的视图看不到之后追加的行；扩容换新缓冲区，旧视图仍有效，搜索拿到的快照始终一致。
    def _set_mem(self, ids, kinds, full, coarse):
        self._buf = [ids, kinds, full, coarse]
        self._n = len(ids)
        self._positions = {int(item_id): pos for pos, item_id in enumerate(ids)}
        self._publish()

    def _publish(self):
        n = self._n
        self.ids, self.kinds, self.full, self.coarse = (b[:n] for b in self._buf)

    def _append_mem(self, ids, kinds, vecs16):
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
            b_ids, b_kinds, b_full, b_coarse = self._buf
            b_ids[n:n + m] = ids
            b_kinds[n:n + m] = kinds
            b_full[n:n + m] = vecs16
            b_coarse[n:n + m] = _normalize(vecs16[:, : config.COARSE_DIM].astype(np.float32))
            for offset, item_id in enumerate(ids):
                self._positions[int(item_id)] = n + offset
            self._n = n + m
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
            rows = self.db.execute("SELECT id, path FROM files WHERE hash=? AND modality=? AND error IS NULL",
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
            self.put_file(path, mtime, size, modality, items, vecs, fp=fp)
            return True

    def put_file(self, path: str, mtime: float, size: int, modality: str, items: list[dict],
                 vecs: np.ndarray | None, error: str | None = None, fp: str | None = None):
        """写入（或替换）一个文件的全部 item。vecs: (len(items), 768) 已归一化。fp：内容指纹。"""
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
                    "INSERT INTO files(path, mtime, size, modality, error, hash) VALUES(?,?,?,?,?,?)",
                    (path, mtime, size, modality, error, fp))
                fid = cur.lastrowid
                if items:
                    self.db.executemany(
                        "INSERT INTO items(file_id, kind, start, end, snippet, vec) VALUES(?,?,?,?,?,?)",
                        [(fid, it["kind"], it.get("start"), it.get("end"), it.get("snippet"), v.tobytes())
                         for it, v in zip(items, vecs16)])
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            self._remove_mem(old_item_ids)
            if items:
                new_ids = np.fromiter(
                    (r[0] for r in self.db.execute("SELECT id FROM items WHERE file_id=? ORDER BY id", (fid,))),
                    dtype=np.int64, count=len(items))
                self._append_mem(new_ids, [it["kind"] for it in items], vecs16)

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
               lex: dict[int, float] | None = None, boost: float = config.LEXICAL_BOOST) -> list[Hit]:
        """两阶段检索：256d 粗排 → 768d 精排。q: (768,) 已归一化。exclude_path：排除查询文件自身（找相似）。
        lex：lexical(文字查询) 的结果，命中的条目（不在向量候选里的也补进来）得分加 boost × 命中强度。
        各类型标签共用同一个查询，lex 只需算一次。"""
        q = np.asarray(q, dtype=np.float32).reshape(-1)
        lex = lex if boost > 0 else None
        with self.lock:
            ids, item_kinds, full, coarse = self.ids, self.kinds, self.full, self.coarse
            if not len(ids):
                return []
            mask = np.isin(item_kinds, list(kinds)) if kinds else None
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
            fine = full[cand].astype(np.float32) @ q + bonus
            fine_order = np.argsort(-fine)
            ordered_ids = ids[cand[fine_order]].copy()
            ordered_scores = fine[fine_order].copy()
            ordered_lex = bonus[fine_order] > 0
        return self._hits(ordered_ids, ordered_scores, k, dedup_files, exclude_path, ordered_lex)

    def lexical(self, text: str) -> dict[int, float]:
        """关键词命中：{item_id: 强度 0~1}。查询按空白拆词，所有词都要出现（AND）。
        - 正文：按 bm25 相对最佳结果的比例给强度。
        - 文件：所有词都出现在路径里、且至少一个词在文件名里（「项目A 报价」这样带上文件夹也能找到）。
          强度看查询词占文件名主干的比例（搜的就是这个文件名时为 1），以及命中的文件少不少（单号、编号这类
          只有一两个文件含有的词也算精确命中），取较大者。
        trigram 只能索引 3 个字符以上的词：有长词时用 FTS 查，短词（如两个字的中文词）只在路径上补充过滤；
        全是短词时只在路径里做子串扫描（路径表小，扫描很快），不查正文。"""
        # 单个英文字母/数字几乎每个路径都有，不算关键词
        terms = list(dict.fromkeys(t.lower() for t in text.split() if len(t) > 1 or not t.isascii()))
        long_terms = [t for t in terms if len(t) >= 3]
        if not terms:
            return {}
        n = config.LEXICAL_CANDIDATES
        out: dict[int, float] = {}
        with self.lock:
            if long_terms:
                match = " AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
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
            if files:
                specific = 3 / len(files)
                fids = list(files)
                q = ",".join("?" * len(fids))
                for iid, fid in self.db.execute(f"SELECT id, file_id FROM items WHERE file_id IN ({q})", fids):
                    out[iid] = max(out.get(iid, 0.0), min(1.0, max(files[fid], specific, 0.2)))
        return out

    def _hits(self, item_ids, scores, k, dedup_files, exclude_path=None, lexical=None) -> list[Hit]:
        out, seen = [], set()
        id_list = [int(i) for i in item_ids]
        if not id_list:
            return out
        q = ",".join("?" * len(id_list))
        with self.lock:
            rows = {r[0]: r for r in self.db.execute(
                f"SELECT items.id, files.path, items.kind, items.start, items.end, items.snippet, files.id, files.mtime "
                f"FROM items JOIN files ON files.id=items.file_id WHERE items.id IN ({q})", id_list)}
        skip = os.path.normcase(os.path.abspath(exclude_path)) if exclude_path else None
        for i, (iid, s) in enumerate(zip(id_list, scores)):
            r = rows.get(iid)
            if r is None:
                continue
            if dedup_files and r[6] in seen:
                continue
            seen.add(r[6])
            if skip and os.path.normcase(r[1]) == skip:
                continue
            out.append(Hit(iid, float(s), r[1], r[2], r[3], r[4], r[5], r[7], bool(lexical is not None and lexical[i])))
            if len(out) >= k:
                break
        return out
