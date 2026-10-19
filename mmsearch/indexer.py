"""增量索引：扫描文件夹 → 对比 mtime/size → 解析媒体 → 编码 → 写入存储。

内容没变的文件（移动、改名、复制，或者只是 mtime 变了）按内容指纹直接复用已有向量，不重新编码。
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

import numpy as np

from . import config, media
from .embedder import Cancelled, EmbedError, Embedder
from .store import VectorStore

IMAGE_BATCH = 4  # 每个任务的图片数；小一些进度更新更及时
# 小文本文件按估算块数合成一个任务：只有一块的小文件逐个发时核显吃不饱，两两成批每块快约 1/4
TEXT_BATCH_CHUNKS = 8
WORKERS = 2  # 一个线程解码媒体时另一个在推理，让模型保持忙碌
# 小批量请求 + 同一时间只有一个请求在推理（embedder.background）：llama-server 的两个槽位总有一个
# 空着留给搜索。否则它会把一个请求里的多张图片拆成多个任务排队，索引期间一次查询要等几十秒。
INDEX_REQ_BATCH = {"text": 4, "image": 1, "audio": 1, "video": 1}
FULL_HASH_BYTES = 64 << 20  # 文本、图片本来就要整个读进来，指纹直接算全文
HASH_WINDOW = 1 << 20  # 音视频（和超大图片）只读开头、中间、结尾各 1 MB，再加上文件大小


def scan(folders: list[str], recursive: bool = True):
    """产出 (路径, 模态, stat)。Windows 上 DirEntry.stat() 直接来自目录枚举，
    不必再逐个文件打开查询：几千个文件的增量扫描快 5~25 倍。"""
    stack = list(folders)
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir():
                        if recursive and e.name not in config.SKIP_DIRS and not e.name.startswith(".") \
                                and not e.is_symlink():
                            stack.append(e.path)
                        continue
                    mod = config.modality_of(Path(e.name))
                    if mod and not e.name.startswith("~$"):  # Office 编辑时的锁文件
                        yield Path(e.path), mod, e.stat()
                except OSError:
                    continue


def _within_folders(scope, folders: list[str]) -> set[tuple[str, bool]]:
    """只保留在已添加文件夹里面的目录（监听回调和移除文件夹可能交错），并去掉被递归扫描覆盖的重复项。"""
    norm = lambda d: os.path.normcase(d.rstrip("\\/"))
    under = lambda d, r: d == r or d.startswith(r + os.sep)
    roots = [norm(f) for f in folders]
    scope = {(d, rec) for d, rec in scope if any(under(norm(d), r) for r in roots)}
    deep = {norm(d) for d, rec in scope if rec}
    covered = lambda d, rec: any(under(norm(d), r) and (norm(d), rec) != (r, True) for r in deep)
    return {(d, rec) for d, rec in scope if not covered(d, rec)}


def _scope_test(scope: set[tuple[str, bool]]) -> Callable[[str], bool]:
    """路径是否落在 scope 里：递归的看前缀，不递归的只看直接所在目录。"""
    flat = {os.path.normcase(d) for d, rec in scope if not rec}
    deep = tuple(os.path.normcase(d.rstrip("\\/")) + os.sep for d, rec in scope if rec)
    return lambda p: os.path.normcase(os.path.dirname(p)) in flat or os.path.normcase(p).startswith(deep)


def fingerprint(path: Path, mod: str, size: int) -> str:
    """内容指纹：认出移动、改名、复制过或者只是 mtime 变了的文件。"""
    h = hashlib.blake2b(size.to_bytes(8, "little"), digest_size=16)
    with open(path, "rb") as f:
        if mod in ("text", "image") and size <= FULL_HASH_BYTES or size <= 3 * HASH_WINDOW:
            while block := f.read(1 << 20):
                h.update(block)
        else:
            for off in (0, (size - HASH_WINDOW) // 2, size - HASH_WINDOW):
                f.seek(off)
                h.update(f.read(HASH_WINDOW))
    return h.hexdigest()


def _prefetch(gen):
    """在后台线程里提前取 gen 的下一个元素：CPU 准备下一段时，GPU 在编码这一段。"""
    ex = ThreadPoolExecutor(1)
    try:
        fut = ex.submit(next, gen, None)
        while (item := fut.result()) is not None:
            fut = ex.submit(next, gen, None)
            yield item
    finally:
        ex.shutdown(wait=True)  # 后台那一步做完才能关闭生成器
        gen.close()


class Indexer:
    def __init__(self, store: VectorStore, embedder: Embedder):
        self.store, self.emb = store, embedder
        self.cancelled = False
        embedder.should_stop = lambda: self.cancelled
        embedder.batch = INDEX_REQ_BATCH
        embedder.background = True

    def run(self, progress: Callable[[int, int, str], None] = lambda *a: None,
            scope: set[tuple[str, bool]] | None = None):
        """progress(done, total, message)。模型服务出错时抛出 EmbedError。
        scope：只重新扫描这些 (目录, 是否递归)（文件夹监听发现的变化）；None 扫描全部已添加的文件夹。"""
        self.cancelled = False
        known = self.store.file_states()
        if scope is None:
            entries = scan(self.store.folders())
        else:  # 只对比 scope 里的已索引文件：其他地方的文件没被扫描，不能当作已删除
            scope = _within_folders(scope, self.store.folders())
            entries = (e for d, rec in scope for e in scan([d], rec))
            inside = _scope_test(scope)
            known = {p: k for p, k in known.items() if inside(p)}
        todo, alive = [], set()
        for p, mod, st in entries:
            sp = str(p)
            alive.add(sp)
            k = known.get(sp)
            if k and abs(k[1] - st.st_mtime) < 1e-6 and k[2] == st.st_size:
                continue
            todo.append((p, mod, st))
        gone = [fid for path, (fid, _, _) in known.items() if path not in alive]
        # 消失的文件先从搜索结果里拿掉，记录留到本轮结束再删：移动/改名到新位置的文件还能复用它们的向量
        self.store.hide_files(gone)
        try:
            self._index_todo(todo, len(gone), progress)
        finally:
            self.store.delete_files(gone)

    def _index_todo(self, todo, n_gone, progress):
        # 便宜的先做：文本 → 图片 → 音频 → 视频，让结果尽早可搜
        order = {"text": 0, "image": 1, "audio": 2, "video": 3}
        todo.sort(key=lambda t: (order[t[1]], t[2].st_size))
        # 图片每 IMAGE_BATCH 张、小文本每 TEXT_BATCH_CHUNKS 块（按大小估算）合成一个任务，其余一个文件一个任务
        limit = {"image": IMAGE_BATCH, "text": TEXT_BATCH_CHUNKS}
        jobs, group, used = [], [], 0
        for t in todo:
            mod = t[1]
            cost = 1 if mod == "image" else max(1, -(-t[2].st_size // config.TEXT_CHUNK_CHARS))
            if group and (mod != group[0][1] or mod not in limit or used + cost > limit[mod]):
                jobs.append(group)
                group, used = [], 0
            group.append(t)
            used += cost
        if group:
            jobs.append(group)

        total, done = len(todo), 0
        progress(0, total, f"待索引 {total} 个文件，清理已删除 {n_gone} 个")
        with ThreadPoolExecutor(WORKERS) as pool:
            pending, it = set(), iter(jobs)
            try:
                while True:
                    while len(pending) < WORKERS * 2 and not self.cancelled:
                        job = next(it, None)
                        if job is None:
                            break
                        pending.add(pool.submit(self._run_job, job))
                    if not pending:
                        break
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for f in finished:
                        n, last = f.result()  # EmbedError 在这里抛出，终止整个索引
                        done += n
                        progress(done, total, last)
            except EmbedError:
                self.cancelled = True
                for f in pending:
                    f.cancel()
                raise
        progress(done, total, "已取消" if self.cancelled else "索引完成")

    # ------------------------------------------------------------
    def _run_job(self, job) -> tuple[int, str]:
        try:
            return self._do_job(job)
        except Cancelled:  # 未完成的文件不记录，下次重新处理
            return 0, ""

    def _do_job(self, job) -> tuple[int, str]:
        if self.cancelled:
            return 0, ""
        work, dups, seen = [], [], set()
        for p, mod, st in job:
            name = p.name if mod == "text" else None  # 文本的向量里带着文件名（title）
            max_bytes = config.MAX_FILE_BYTES.get(mod)
            try:
                if max_bytes and st.st_size > max_bytes:
                    raise ValueError("文件过大")
                fp = fingerprint(p, mod, st.st_size)
            except (OSError, ValueError) as e:
                self.store.put_file(str(p), st.st_mtime, st.st_size, mod, [], None, error=repr(e))
                continue
            if self.store.reuse(str(p), st.st_mtime, st.st_size, mod, fp, name):
                continue
            if (fp, name) in seen:
                dups.append((p, mod, st, fp, name))
            else:
                seen.add((fp, name))
                work.append((p, mod, st, fp))
        handle = {"text": self._index_texts, "image": self._index_images}.get(job[0][1], self._index_media)
        if work:
            handle(work)
        for p, mod, st, fp, name in dups:  # 同一任务里内容相同的文件：前一个写完就能复用（它出错了才各自处理）
            if not self.store.reuse(str(p), st.st_mtime, st.st_size, mod, fp, name):
                handle([(p, mod, st, fp)])
        return len(job), str(job[-1][0])

    def _index_texts(self, files):
        docs, ok = [], []
        for p, mod, st, fp in files:
            try:
                chunks, pages = self._text_parts(p)
            except Exception as e:
                traceback.print_exc()
                self.store.put_file(str(p), st.st_mtime, st.st_size, "text", [], None, error=repr(e), fp=fp)
                continue
            ok.append((p, st, fp, chunks, pages, len(docs)))
            docs += [(p.name, c) for c in chunks]
        vecs = self.emb.encode_titled(docs) if docs else None  # 多个小文件的块合进同一批请求
        for p, st, fp, chunks, pages, s in ok:
            items = [{"kind": "text", "snippet": c} for c in chunks]
            parts = [vecs[s: s + len(chunks)]] if chunks else []
            if pages:  # 扫描页按图片编码，start 记页码（从 1 开始），缩略图和预览按页码取图
                items += [{"kind": "page", "start": i + 1, "snippet": f"第 {i + 1} 页"} for i, _ in pages]
                parts.append(self.emb.encode_images([img for _, img in pages]))
            self.store.put_file(str(p), st.st_mtime, st.st_size, "text", items,
                                np.concatenate(parts) if parts else None, fp=fp)

    @staticmethod
    def _text_parts(p: Path) -> tuple[list[str], list]:
        """(文本块, 扫描页 [(页码, 图片)])。只有 PDF 会有扫描页；什么都读不出的 PDF 抛出说明原因的错误。"""
        if p.suffix.lower() != ".pdf":
            return media.file_chunks(p), []
        text, empty = media.read_pdf(p)
        chunks = media.chunk_text(text)
        pages = media.pdf_scan_pages(p, empty[: config.SCANNED_PDF_MAX_PAGES]) if empty else []
        if not chunks and not pages and empty:
            raise ValueError("PDF 没有文字层，页面也不是整页扫描图片（可能是画成矢量轮廓的文字），读不出内容")
        return chunks, pages

    def _index_images(self, batch):
        imgs, ok = [], []
        for p, mod, st, fp in batch:
            try:
                imgs.append(media.load_image(p))
                ok.append((p, st, fp))
            except Exception as e:
                self.store.put_file(str(p), st.st_mtime, st.st_size, "image", [], None, error=repr(e), fp=fp)
        if not imgs:
            return
        vecs = self.emb.encode_images(imgs)
        for (p, st, fp), v, img in zip(ok, vecs, imgs):
            self.store.put_file(str(p), st.st_mtime, st.st_size, "image",
                                [{"kind": "image", "snippet": p.name}], v[None], fp=fp, dhash=media.dhash(img))

    def _index_media(self, files):
        p, mod, st, fp = files[0]  # 音视频一个文件一个任务
        try:
            items, vecs = self._process(p, mod)
        except (EmbedError, Cancelled):
            raise
        except Exception as e:
            traceback.print_exc()
            self.store.put_file(str(p), st.st_mtime, st.st_size, mod, [], None, error=repr(e), fp=fp)
        else:
            self.store.put_file(str(p), st.st_mtime, st.st_size, mod, items, vecs, fp=fp)

    def _process(self, p: Path, mod: str):
        if mod == "audio":
            segs = media.split_audio(media.load_audio(p))
            if not segs:
                return [], None
            vecs = self.emb.encode_audios([s[2] for s in segs])
            return [{"kind": "audio", "start": s[0], "end": s[1]} for s in segs], vecs

        if mod == "video":
            items, vecs = [], []
            last = None  # (小图, 向量)：最近一次真正编码的画面段。和它比（而不是和上一段比），缓慢变化不会被一路复用下去
            # 逐段编码，下一段的帧（最后是音轨）在后台线程里同时解码：GPU 不用等抽帧，长视频也不会占满内存
            with contextlib.closing(_prefetch(self._video_units(p))) as units:
                for kind, start, end, data in units:
                    if kind == "video":
                        small = media.frame_thumbs(data)
                        if last is None or media.frames_diff(small, last[0]) >= config.VIDEO_STATIC_DIFF:
                            last = (small, self.emb.encode_videos([data]))
                        vecs.append(last[1])  # 画面没变（录屏、静止镜头）直接复用
                    else:
                        vecs.append(self.emb.encode_audios([data]))
                    items.append({"kind": kind, "start": start, "end": end})
            return (items, np.concatenate(vecs)) if items else ([], None)
        raise ValueError(mod)

    @staticmethod
    def _video_units(p: Path):
        """视频的编码单元：先逐段画面，再逐段音轨（音轨单独编码：支持按台词/声音搜视频）。"""
        for start, end, frames in media.video_segments(p):
            yield "video", start, end, frames
        a = media.load_audio(p, max_sec=config.VIDEO_MAX_SEGMENTS * config.AUDIO_SEGMENT_SEC)
        for start, end, piece in media.split_audio(a):
            if np.abs(piece).mean() > 1e-3:  # 跳过静音
                yield "video_audio", start, end, piece
