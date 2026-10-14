"""模型下载：断点续传、进度回调、可取消。"""
from __future__ import annotations

import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import config


class DownloadCancelled(Exception):
    pass


def model_urls(endpoint: str) -> list[tuple[str, str]]:
    return [(f"{endpoint}/{config.GGUF_REPO}/resolve/main/{n}", n) for n in config.missing_models()]


def download_models(endpoint: str, progress: Callable[[int, int, str], None],
                    cancelled: Callable[[], bool] = lambda: False):
    """progress(已下载字节, 总字节, 当前文件名)。完成后文件位于 config.model_download_dir()。"""
    jobs = model_urls(endpoint)
    dest_dir = config.model_download_dir()
    total = sum(config.MODEL_SIZES.get(n, 0) for _, n in jobs)
    done_before = 0
    for url, name in jobs:
        part = dest_dir / (name + ".part")
        have = part.stat().st_size if part.exists() else 0
        if have and have == config.MODEL_SIZES.get(name):  # 上次已下完，只差改名
            part.replace(dest_dir / name)
            done_before += have
            continue
        req =urllib.request.Request(url, headers={"User-Agent": "mmsearch", "Range": f"bytes={have}-"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status == 200:  # 服务器不支持续传，从头开始
                have = 0
            size = have + int(r.headers.get("Content-Length", 0))
            if name not in config.MODEL_SIZES:
                total += size
            mode = "ab" if have else "wb"
            last = 0.0
            with open(part, mode) as f:
                while True:
                    if cancelled():
                        raise DownloadCancelled()
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    have += len(chunk)
                    if time.time() - last > 0.2:
                        progress(done_before + have, total, name)
                        last = time.time()
        expected = config.MODEL_SIZES.get(name)
        if expected and part.stat().st_size != expected:
            raise IOError(f"{name} 大小不符（{part.stat().st_size} != {expected}），请重试")
        part.replace(dest_dir / name)
        done_before += have
        progress(done_before, total, name)
