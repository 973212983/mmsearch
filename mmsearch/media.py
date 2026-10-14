"""媒体解析：文本抽取与切块、音频解码重采样、视频分段抽帧。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from . import config


# ---------------- 文本 ----------------
def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    if ext == ".docx":
        import zipfile
        import re
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def chunk_text(text: str, size=config.TEXT_CHUNK_CHARS, overlap=config.TEXT_CHUNK_OVERLAP,
               limit: int | None = config.TEXT_MAX_CHUNKS_PER_FILE) -> list[str]:
    """按段落聚合切块，超长段落硬切。limit=None 不限块数。"""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    chunks, buf = [], ""
    for para in text.split("\n"):
        i = 0
        while len(para) - i > size:  # 用下标前进：压缩过的 JSON 这类超长单行，反复切片复制会是平方复杂度
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[i: i + size])
            i += size - overlap
        para = para[i:]
        if len(buf) + len(para) + 1 > size:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""
        buf = f"{buf}\n{para}" if buf else para
    if buf.strip():
        chunks.append(buf)
    chunks = [c.strip() for c in chunks if c.strip()]
    return chunks[:limit]


def file_chunks(path: Path) -> list[str]:
    """索引一个文本文件时要编码的块。DATA_EXT 类文件开头、中间、结尾各取 DATA_CHUNKS_EACH 块。"""
    text = read_text(path)
    if path.suffix.lower() not in config.DATA_EXT:
        return chunk_text(text)
    chunks, n = chunk_text(text, limit=None), config.DATA_CHUNKS_EACH
    if len(chunks) <= 3 * n:
        return chunks
    mid = (len(chunks) - n) // 2
    return chunks[:n] + chunks[mid: mid + n] + chunks[-n:]


# ---------------- 音频 ----------------
def load_audio(path: Path, sr: int = config.AUDIO_SR, max_sec: float | None = None) -> np.ndarray:
    """用 PyAV 解码任意音频（或视频的音轨）为 16kHz 单声道 float32。无音轨返回空数组。"""
    import av
    out = []
    with av.open(str(path)) as c:
        if not c.streams.audio:
            return np.zeros(0, dtype=np.float32)
        stream = c.streams.audio[0]
        res = av.AudioResampler(format="flt", layout="mono", rate=sr)
        total = 0
        for frame in c.decode(stream):
            for f in res.resample(frame):
                a = f.to_ndarray().reshape(-1)
                out.append(a)
                total += len(a)
            if max_sec and total >= max_sec * sr:
                break
        for f in res.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    a = np.concatenate(out).astype(np.float32) if out else np.zeros(0, dtype=np.float32)
    return a[: int(max_sec * sr)] if max_sec else a


def split_audio(a: np.ndarray, sr=config.AUDIO_SR, seg=config.AUDIO_SEGMENT_SEC):
    """切成固定长度片段；末尾过短（<3s）的并入前一段。"""
    n = int(seg * sr)
    segs = []
    for s in range(0, len(a), n):
        piece = a[s: s + n]
        if len(piece) < 3 * sr and segs:
            st, _, prev = segs[-1]
            segs[-1] = (st, (s + len(piece)) / sr, np.concatenate([prev, piece]))
        elif len(piece) >= sr // 2:
            segs.append((s / sr, (s + len(piece)) / sr, piece))
    return segs


# ---------------- 视频 ----------------
def _grab(c, vs, t: float, max_side: int) -> Image.Image | None:
    """seek 到 t 之前的关键帧，再向前解码到 t。"""
    tb = vs.time_base
    try:
        c.seek(int(t / tb), stream=vs, backward=True)
        for fr in c.decode(vs):
            if fr.pts is None or fr.pts * tb >= t - 0.05:
                img = fr.to_image()
                img.thumbnail((max_side, max_side))
                return img
    except Exception:
        pass
    return None


def sample_frames(path: Path, times: list[float], max_side: int = 768) -> list[Image.Image | None]:
    """按时间点抽帧（seek 到关键帧后向前解码到目标时间）。"""
    import av
    with av.open(str(path)) as c:
        vs = c.streams.video[0]
        vs.thread_type = "AUTO"
        return [_grab(c, vs, t, max_side) for t in times]


def video_segments(path: Path, seg=config.VIDEO_SEGMENT_SEC, per_seg=config.VIDEO_FRAMES_PER_SEGMENT):
    """逐段产出 (start, end, [PIL帧...])。生成器：整个视频只打开一次，
    索引时可以边抽帧边编码，长视频也只在内存里留当前这几段的帧。"""
    import av
    with av.open(str(path)) as c:
        vs = c.streams.video[0]
        vs.thread_type = "AUTO"
        if c.duration:
            dur = c.duration / 1_000_000
        else:
            dur = float(vs.duration * vs.time_base) if vs.duration else 0.0
        if dur <= 0:
            f = _grab(c, vs, 0.0, 768)
            if f is not None:
                yield 0.0, 0.0, [f]
            return
        n_seg = min(config.VIDEO_MAX_SEGMENTS, max(1, int(np.ceil(dur / seg))))
        seg = dur / n_seg if dur / n_seg > seg else seg
        produced = False
        for i in range(n_seg):
            st, en = i * seg, min(dur, (i + 1) * seg)
            if en - st < 1.0 and produced:
                break
            k = max(1, min(per_seg, int(en - st)))
            frames = [_grab(c, vs, st + (en - st) * (j + 0.5) / k, 768) for j in range(k)]
            frames = [f for f in frames if f is not None]
            if frames:
                produced = True
                yield st, en, frames


def load_image(path: Path, max_side: int = 1024) -> Image.Image:
    """默认 1024：送进视觉编码器的图片最长边就是 1024（embedder.encode_images）。"""
    img = Image.open(path)
    w, h = img.size
    if max(w, h) > max_side:  # JPEG 直接按目标尺寸降采样解码（按长边算，竖图横图都能降到最多）
        r = max_side / max(w, h)
        img.draft("RGB", (max(1, int(w * r)), max(1, int(h * r))))
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    return img
