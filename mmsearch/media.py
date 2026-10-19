"""媒体解析：文本抽取与切块、音频解码重采样、视频分段抽帧。"""
from __future__ import annotations

import html
import posixpath
import re
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from . import config


# ---------------- 文本 ----------------
def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return read_pdf(path)[0]
    if ext in _ZIP_READERS:
        with zipfile.ZipFile(path) as z:
            return _ZIP_READERS[ext](z)
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _xml_text(xml: str, para: str) -> str:
    """去掉标签取文字：para 段落结束标签换行。"""
    xml = re.sub(para, "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def _read_docx(z: zipfile.ZipFile) -> str:
    return _xml_text(z.read("word/document.xml").decode("utf-8", "ignore"), r"</w:p>")


def _read_pptx(z: zipfile.ZipFile) -> str:
    """每页幻灯片的文字（含备注），按页码顺序，页之间空一行。"""
    num = lambda n: int(re.search(r"(\d+)\.xml$", n)[1])
    slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml", n)),
                    key=lambda n: (num(n), "notes" in n))
    return "\n\n".join(_xml_text(z.read(n).decode("utf-8", "ignore"), r"</a:p>").strip() for n in slides)


def _read_xlsx(z: zipfile.ZipFile) -> str:
    """每个工作表一段（## 表名），每行单元格用制表符隔开。"""
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    names = set(z.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = []
    for sheet in ET.fromstring(z.read("xl/workbook.xml")).iter("{%s}sheet" % ns["m"]):
        target = rels.get(sheet.get(rid), "")
        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
        if target not in names:
            continue
        rows = [f"## {sheet.get('name')}"]
        for row in ET.fromstring(z.read(target)).iter("{%s}row" % ns["m"]):
            cells = []
            for c in row.findall("m:c", ns):
                v, t = c.find("m:v", ns), c.get("t")
                if t == "s" and v is not None:
                    cells.append(shared[int(v.text)] if v.text and int(v.text) < len(shared) else "")
                elif t == "inlineStr":
                    cells.append("".join(x.text or "" for x in c.iter("{%s}t" % ns["m"])))
                else:
                    cells.append((v.text or "") if v is not None else "")
            if any(cells):
                rows.append("\t".join(cells).rstrip())
        out.append("\n".join(rows))
    return "\n\n".join(out)


def _read_epub(z: zipfile.ZipFile) -> str:
    """按 OPF 里 spine 的阅读顺序取各章节 XHTML 的文字。"""
    opf = re.search(r'full-path="([^"]+)"', z.read("META-INF/container.xml").decode("utf-8", "ignore"))[1]
    doc = z.read(opf).decode("utf-8", "ignore")
    base = posixpath.dirname(opf)
    hrefs = {m[1]: m[2] for m in re.finditer(r'<item\b[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', doc)}
    hrefs.update({m[2]: m[1] for m in re.finditer(r'<item\b[^>]*?href="([^"]+)"[^>]*?id="([^"]+)"', doc)})
    out = []
    for idref in re.findall(r'<itemref\b[^>]*?idref="([^"]+)"', doc):
        name = posixpath.normpath(posixpath.join(base, html.unescape(hrefs.get(idref, ""))))
        if name in z.namelist():
            x = z.read(name).decode("utf-8", "ignore")
            x = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", x)
            out.append(_xml_text(x, r"(?i)</(p|div|h[1-6]|li|tr|br)>|<br\s*/?>").strip())
    return "\n\n".join(t for t in out if t)


_ZIP_READERS = {".docx": _read_docx, ".pptx": _read_pptx, ".xlsx": _read_xlsx, ".epub": _read_epub}

# 页面没有文字层（少于这么多字）就当作扫描页，看它是不是整页一张图片
SCAN_MIN_CHARS = 20


def read_pdf(path: Path) -> tuple[str, list[int]]:
    """(全文, 没有文字层的页码列表，从 0 开始)。"""
    from pypdf import PdfReader
    texts = [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
    return "\n".join(texts), [i for i, t in enumerate(texts) if len(t.strip()) < SCAN_MIN_CHARS]


def pdf_page_image(path: Path, page: int) -> Image.Image | None:
    """扫描页的整页图片（页面里最大的一张图，且尺寸和比例都像整页）；不是整页图片时返回 None。
    不渲染矢量内容（那需要单独的 PDF 渲染引擎），所以文字画成矢量轮廓的 PDF 读不出来。"""
    from pypdf import PdfReader
    pg = PdfReader(str(path)).pages[page]
    return _full_page_image(pg)


def _full_page_image(pg) -> Image.Image | None:
    try:
        imgs = [im.image for im in pg.images]
    except Exception:
        return None
    if not imgs:
        return None
    img = max(imgs, key=lambda im: im.width * im.height)
    w, h = float(pg.mediabox.width), float(pg.mediabox.height)
    if pg.rotation % 180:
        w, h = h, w
    if max(img.size) < 800 or abs(img.width / img.height - w / h) > 0.15 * (w / h):
        return None
    return img.convert("RGB")


def pdf_scan_pages(path: Path, pages: list[int]) -> list[tuple[int, Image.Image]]:
    """这些页里能取到整页图片的 (页码, 图片)。"""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    out = []
    for i in pages:
        img = _full_page_image(reader.pages[i])
        if img is not None:
            img.thumbnail((1024, 1024))
            out.append((i, img))
    return out


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


def frame_thumbs(frames: list[Image.Image]) -> np.ndarray:
    """一段画面的 64×64 灰度小图，用于 frames_diff。"""
    return np.stack([np.asarray(f.convert("L").resize((64, 64)), np.float32) for f in frames])


def frames_diff(a: np.ndarray, b: np.ndarray) -> float:
    """两段画面（frame_thumbs）逐帧的平均绝对像素差 / 255，取差别最大的一帧；帧数不同时返回 1。"""
    if a.shape != b.shape:
        return 1.0
    return float(np.abs(a - b).mean(axis=(1, 2)).max() / 255)


def load_image(path: Path, max_side: int = 1024) -> Image.Image:
    """默认 1024：送进视觉编码器的图片最长边就是 1024（embedder.encode_images）。"""
    img = Image.open(path)
    w, h = img.size
    if max(w, h) > max_side:  # JPEG 直接按目标尺寸降采样解码（按长边算，竖图横图都能降到最多）
        r = max_side / max(w, h)
        img.draft("RGB", (max(1, int(w * r)), max(1, int(h * r))))
    img = to_rgb(img)
    img.thumbnail((max_side, max_side))
    return img


def dhash(img: Image.Image) -> int:
    """64 位差值哈希（缩到 9×8 灰度，每行相邻像素比亮度），存成有符号 64 位整数。用来找「几乎相同」的图：
    同一张图缩放、重新压缩后汉明距离不超过 6，不同的图基本都在 20 以上。几乎纯色的图返回 0（不参与比较）。"""
    g = img.convert("L")
    if np.asarray(g.resize((32, 32)), np.float32).std() < 2:
        return 0
    g = np.asarray(g.resize((9, 8), Image.LANCZOS), np.int16)
    bits = np.packbits(g[:, 1:] > g[:, :-1])
    return int(bits.view(">i8")[0])


def to_rgb(img: Image.Image) -> Image.Image:
    """透明图先铺到和内容反差大的底色上再转 RGB。直接 convert('RGB') 会露出透明处底下的颜色：
    很多图标透明处和图案本身的 RGB 一样（常见全黑），转完就成了一整块纯色，编码出来什么都不是。"""
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode not in ("RGBA", "LA", "PA"):
        return img.convert("RGB")
    img = img.convert("RGBA")
    a = np.asarray(img.getchannel("A"), np.float32)
    if a.min() == 255:
        return img.convert("RGB")
    lum = np.asarray(img.convert("L"), np.float32)
    dark = a.sum() == 0 or (lum * a).sum() / a.sum() < 128  # 图案偏暗铺白底，偏亮铺深灰底
    bg = Image.new("RGB", img.size, (255, 255, 255) if dark else (40, 40, 40))
    bg.paste(img, mask=img.getchannel("A"))
    return bg
