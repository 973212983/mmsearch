"""本地 embedding：由应用托管的 llama-server（llama.cpp + EmbeddingGemma 2 GGUF）。

- 文本模型 GGUF + mmproj（视觉/音频编码器）
- 优先 Vulkan（核显），不可用时回退 CPU
- 通过 OpenAI 兼容的 /v1/embeddings 调用，多模态输入用 content parts
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import io
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

from . import config

# 每个请求的最大条数：llama-server 逐条编码，批量主要省 HTTP 往返
REQ_BATCH = {"text": 32, "image": 8, "audio": 4, "video": 1}


class EmbedError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


def _jpeg_url(img: Image.Image, max_side: int, quality: int = 90) -> str:
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _wav_b64(a: np.ndarray, sr: int = config.AUDIO_SR) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_JOB = None


def _kill_with_parent(proc: subprocess.Popen):
    """把子进程放进 Job Object（KILL_ON_JOB_CLOSE）：主进程被强制结束时它也随之退出，不会残留。"""
    global _JOB
    import ctypes
    import ctypes.wintypes as wt

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.OpenProcess.restype = ctypes.c_void_p
    if _JOB is None:
        _JOB = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(ctypes.c_void_p(_JOB), 9, ctypes.byref(info), ctypes.sizeof(info))
    h = k32.OpenProcess(0x0101, False, proc.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if h:
        k32.AssignProcessToJobObject(ctypes.c_void_p(_JOB), ctypes.c_void_p(h))
        k32.CloseHandle(ctypes.c_void_p(h))


class LlamaServer:
    """托管 llama-server 子进程。"""

    def __init__(self, backend: str = "auto", image_tokens: int = 280):
        self.backend = backend
        self.image_tokens = image_tokens
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.device = ""
        self.lock = threading.Lock()
        self.log_path = config.DATA_DIR / "llama-server.log"
        self._epoch = 0  # stop() 递增；进行中的启动发现变化后放弃（设置里切换设备时不会和旧的启动打架）

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _candidates(self):
        order = {"auto": ["vulkan", "cpu"], "vulkan": ["vulkan"], "cpu": ["cpu"]}[self.backend]
        for b in order:
            exe = config.RUNTIME_DIR / f"llama-{b}" / "llama-server.exe"
            if exe.exists():
                yield b, exe

    def ensure_started(self, on_status=lambda s: None):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            missing = config.missing_models()
            if missing:
                raise EmbedError("缺少模型文件：" + "、".join(missing))
            epoch = self._epoch
            errors = []
            for backend, exe in self._candidates():
                if epoch != self._epoch:
                    raise EmbedError("模型启动已取消")
                on_status(f"正在启动模型（{backend}）…")
                try:
                    self._start(backend, exe, epoch)
                    self.device = backend
                    atexit.register(self.stop)
                    return
                except EmbedError as e:
                    errors.append(f"{backend}: {e}")
                    self._kill()
            raise EmbedError("llama-server 启动失败\n" + "\n".join(errors) if errors else
                             f"未找到 llama-server 运行时（{config.RUNTIME_DIR}），请重新安装")

    def _start(self, backend: str, exe: Path, epoch: int):
        self.port = _free_port()
        ctx = config.CTX_PER_SLOT
        args = [str(exe), "-m", str(config.find_model(config.GGUF_NAME)),
                "--mmproj", str(config.find_model(config.MMPROJ_NAME)),
                "--embeddings", "--pooling", "mean",
                "-c", str(ctx * config.PARALLEL), "-b", str(ctx), "-ub", str(ctx),
                "--parallel", str(config.PARALLEL), "--host", "127.0.0.1", "--port", str(self.port),
                "--no-webui", "--image-max-tokens", str(self.image_tokens)] + config.LLAMA_EXTRA_ARGS
        if backend == "vulkan":
            args += ["-ngl", "99"]
        else:
            args += ["-ngl", "0", "--no-mmproj-offload", "-t", str(config.CPU_THREADS)]
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        _kill_with_parent(self.proc)
        deadline = time.time() + 180
        while time.time() < deadline:
            if epoch != self._epoch:
                raise EmbedError("模型启动已取消")
            if self.proc.poll() is not None:
                raise EmbedError(f"进程退出（code {self.proc.returncode}），见 {self.log_path}")
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                    if json.load(r).get("status") == "ok":
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        raise EmbedError("启动超时")

    def _kill(self):
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait(5)
        self.proc = None

    def stop(self):
        self._epoch += 1
        self._kill()


class _QueryGate:
    """搜索优先。llama-server 在主循环里同步编码图片/音频（核显上每张 1.5~4 s），这期间另一个槽位里的
    查询也只能等着，而且无法打断。所以查询进行中、刚结束（用户可能还在继续输入）或面板刚唤出时，
    后台索引先不发新请求，最多只让查询碰上一张正在编码的图片。"""

    GRACE = 3.0  # 查询结束后让出的时间：连续输入时后续查询不再被挡

    def __init__(self):
        self._active = 0
        self._hold_until = 0.0
        self._cv = threading.Condition()

    def hold(self, seconds: float):
        with self._cv:
            self._hold_until = max(self._hold_until, time.monotonic() + seconds)

    @contextlib.contextmanager
    def query(self):
        with self._cv:
            self._active += 1
        try:
            yield
        finally:
            with self._cv:
                self._active -= 1
                self._hold_until = max(self._hold_until, time.monotonic() + self.GRACE)
                self._cv.notify_all()

    def wait_idle(self, max_wait: float = 20.0):  # 有上限：一直在搜索也不会让索引饿死
        deadline = time.monotonic() + max_wait
        with self._cv:
            while True:
                now = time.monotonic()
                if now >= deadline or (self._active == 0 and now >= self._hold_until):
                    return
                until = deadline if self._active else min(deadline, self._hold_until)
                self._cv.wait(max(until - now, 0.01))


_GATE = _QueryGate()


def hold_background(seconds: float):
    """让后台索引暂停发新请求一段时间（面板唤出时调用：用户马上要搜索了）。"""
    _GATE.hold(seconds)


class Embedder:
    def __init__(self, server: LlamaServer, timeout: float = 600):
        self.server = server
        self.timeout = timeout
        self.should_stop = lambda: False  # 索引用：每个请求前检查，便于快速停止
        self.batch = REQ_BATCH  # 每个请求的条数；索引器会改小，见 indexer.py
        self.background = False  # 索引器设为 True：同一时间只发一个请求，并给搜索让路
        self._bg_lock = threading.Lock()
        self._qcache: OrderedDict[tuple, np.ndarray] = OrderedDict()

    def _request(self):
        if not self.background:
            return _GATE.query()
        return self._background_slot()

    @contextlib.contextmanager
    def _background_slot(self):
        with self._bg_lock:
            _GATE.wait_idle()
            yield

    def _embed(self, inputs: list, kind: str) -> np.ndarray:
        self.server.ensure_started()
        out = []
        n = self.batch[kind]
        for s in range(0, len(inputs), n):
            if self.should_stop():
                raise Cancelled()
            body = json.dumps({"input": inputs[s: s + n], "encoding_format": "float"}).encode()
            req = urllib.request.Request(self.server.url + "/v1/embeddings", body,
                                         {"Content-Type": "application/json"})
            try:
                with self._request(), urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.load(r)
            except urllib.error.HTTPError as e:
                raise EmbedError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}") from e
            except (urllib.error.URLError, OSError) as e:
                raise EmbedError(f"llama-server 无响应：{e}") from e
            v = np.array([x["embedding"] for x in sorted(d["data"], key=lambda x: x["index"])], np.float32)
            if v.shape[1] != config.FULL_DIM or not np.isfinite(v).all():
                raise EmbedError(f"异常向量 shape={v.shape}")
            out.append(v / np.linalg.norm(v, axis=1, keepdims=True))
        return np.concatenate(out) if out else np.zeros((0, config.FULL_DIM), np.float32)

    # ---------- 文档侧 ----------
    def encode_documents(self, chunks: list[str], title: str | None = None) -> np.ndarray:
        return self.encode_titled([(title, c) for c in chunks])

    def encode_titled(self, docs: list[tuple[str | None, str]]) -> np.ndarray:
        """(标题, 文本块) 列表：索引时多个小文件的块可以合进同一批请求。"""
        return self._embed([f"title: {(t or 'none').replace('|', ' ')} | text: {c}" for t, c in docs], "text")

    def encode_images(self, imgs: list[Image.Image]) -> np.ndarray:
        return self._embed([{"content": [{"type": "image_url", "image_url": {"url": _jpeg_url(im, 1024)}}]}
                            for im in imgs], "image")

    def encode_audios(self, arrays: list[np.ndarray]) -> np.ndarray:
        return self._embed([{"content": [{"type": "input_audio", "input_audio": {"data": _wav_b64(a), "format": "wav"}}]}
                            for a in arrays], "audio")

    def encode_videos(self, frame_lists: list[list[Image.Image]]) -> np.ndarray:
        # 一个片段的多帧作为同一输入里的多张图片
        return self._embed([{"content": [{"type": "image_url", "image_url": {"url": _jpeg_url(f, 768, 85)}}
                                         for f in fr]} for fr in frame_lists], "video")

    # ---------- 查询侧 ----------
    def encode_query(self, text: str, prefix: str = "task: search result | query: ") -> np.ndarray:
        key = (prefix, text)
        if key in self._qcache:
            self._qcache.move_to_end(key)
            return self._qcache[key]
        v = self._embed([prefix + text], "text")[0]
        self._qcache[key] = v
        if len(self._qcache) > 256:
            self._qcache.popitem(last=False)
        return v
