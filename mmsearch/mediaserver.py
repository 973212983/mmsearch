"""本地媒体服务（仅 127.0.0.1）：缩略图、音频波形、文件流（支持 Range，供 <video>/<audio> 拖动）。

只允许访问已索引文件夹内的文件或显式放行的查询文件，且需要随机 token。
"""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import secrets
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import numpy as np

from . import config, media
from .store import VectorStore

CACHE_DIR = config.DATA_DIR / "cache" / "thumbs"
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("video/mp4", ".m4v")


class MediaServer:
    def __init__(self, store: VectorStore):
        self.store = store
        self.token = secrets.token_urlsafe(16)
        self.allowed_extra: set[str] = set()
        self.wave_cache: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, kind: str, path: str, **params) -> str:
        q = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        return f"{self.base}/{kind}?k={self.token}&p={quote(path)}" + (f"&{q}" if q else "")

    def allow(self, path: str):
        self.allowed_extra.add(os.path.normcase(os.path.abspath(path)))

    def permitted(self, path: str) -> bool:
        p = os.path.normcase(os.path.abspath(path))
        if p in self.allowed_extra:
            return True
        return any(p.startswith(os.path.normcase(os.path.abspath(f)).rstrip("\\/") + os.sep)
                   for f in self.store.folders())

    # ---------------- 内容生成 ----------------
    def thumb(self, path: str, t: float | None, w: int) -> bytes:
        st = os.stat(path)
        key = hashlib.sha1(f"{path}|{st.st_mtime}|{t}|{w}|rgb2".encode()).hexdigest()  # rgb2：透明图改铺底色后，旧缓存作废
        f = CACHE_DIR / f"{key}.jpg"
        if f.exists():
            return f.read_bytes()
        p = Path(path)
        if config.modality_of(p) == "image":
            img = media.load_image(p, max_side=w)
        elif p.suffix.lower() == ".pdf":  # 扫描页：t 是页码（从 1 开始）
            img = media.pdf_page_image(p, int(t or 1) - 1)
            if img is None:
                raise FileNotFoundError("no page image")
            img.thumbnail((w, w))
        else:
            img = media.sample_frames(p, [t or 0.0], max_side=w)[0]
            if img is None:
                raise FileNotFoundError("no frame")
        buf = io.BytesIO()
        media.to_rgb(img).save(buf, "JPEG", quality=84)
        data = buf.getvalue()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return data

    def wave(self, path: str, s: float | None, e: float | None, n: int) -> list[float]:
        """真实波形：片段内 n 个峰值（0~1）。"""
        key = (path, s, e, n)
        with self.lock:
            if key in self.wave_cache:
                return self.wave_cache[key]
        s = s or 0.0
        a = media.load_audio(Path(path), max_sec=(e or s + config.AUDIO_SEGMENT_SEC) + 0.1)
        a = a[int(s * config.AUDIO_SR):]
        if len(a) == 0:
            peaks = [0.0] * n
        else:
            parts = np.array_split(np.abs(a), n)
            peaks = np.array([p.max() if len(p) else 0.0 for p in parts])
            peaks = (peaks / max(peaks.max(), 1e-4)).round(3).tolist()
        with self.lock:
            self.wave_cache[key] = peaks
            if len(self.wave_cache) > 500:
                self.wave_cache.popitem(last=False)
        return peaks

    # ---------------- HTTP ----------------
    def _make_handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _fail(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send(self, data: bytes, ctype: str, cache=True):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")  # 页面与媒体服务端口不同
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                path = q.get("p", "")
                if q.get("k") != srv.token or not path or not srv.permitted(path) or not os.path.isfile(path):
                    return self._fail(404 if q.get("k") == srv.token else 403)
                try:
                    if u.path == "/thumb":
                        t = float(q["t"]) if "t" in q else None
                        return self._send(srv.thumb(path, t, int(q.get("w", 480))), "image/jpeg")
                    if u.path == "/wave":
                        s = float(q["s"]) if "s" in q else None
                        e = float(q["e"]) if "e" in q else None
                        data = json.dumps(srv.wave(path, s, e, int(q.get("n", 48)))).encode()
                        return self._send(data, "application/json")
                    if u.path == "/file":
                        return self._file(path)
                except (ConnectionError, BrokenPipeError):
                    return
                except Exception:
                    return self._fail(500)
                self._fail(404)

            def _file(self, path):
                size = os.path.getsize(path)
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                rng = self.headers.get("Range")
                start, end = 0, size - 1
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    if a:
                        start = int(a)
                        end = int(b) if b else size - 1
                    else:
                        start = max(0, size - int(b))
                    end = min(end, size - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(1 << 16, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)

        return Handler
