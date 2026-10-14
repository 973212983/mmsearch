"""模型下载：断点续传、进度回调、可取消。"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path
from typing import Callable

from . import config


class DownloadCancelled(Exception):
    pass


def model_urls(endpoint: str) -> list[tuple[str, str]]:
    """[(下载地址, 本地文件名)]：仓库内可以带子目录，本地只保留文件名。"""
    src = config.model_source()
    if src.get("kind") != "repo":
        return []  # 本地文件来源没有可下载的东西
    jobs = []
    for name in config.missing_models():
        rel = name.replace("\\", "/")
        jobs.append((f"{endpoint}/{src['repo']}/resolve/main/{quote(rel)}", rel.rsplit("/", 1)[-1]))
    return jobs


def download_models(endpoint: str, progress: Callable[[int, int, str], None],
                    cancelled: Callable[[], bool] = lambda: False):
    """progress(已下载字节, 总字节, 当前文件名)。完成后文件位于 config.model_download_dir()。"""
    jobs = model_urls(endpoint)
    dest_dir = config.model_download_dir()
    total = sum(config.known_size(n) for _, n in jobs)
    done_before = 0
    for url, name in jobs:
        part = dest_dir / (name + ".part")
        have = part.stat().st_size if part.exists() else 0
        if have and have == config.known_size(name):  # 上次已下完，只差改名
            part.replace(dest_dir / name)
            done_before += have
            continue
        req =urllib.request.Request(url, headers={"User-Agent": "mmsearch", "Range": f"bytes={have}-"})
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:  # 大小未知的文件上次已下完：续传范围超出文件末尾
                part.replace(dest_dir / name)
                done_before += have
                continue
            if e.code in (401, 403, 404):
                raise IOError(f"仓库里找不到 {name}，或该仓库需要登录授权（HTTP {e.code}），请检查仓库名和文件名") from e
            raise
        with r:
            if r.status == 200:  # 服务器不支持续传，从头开始
                have = 0
            size = have + int(r.headers.get("Content-Length", 0))
            if not config.known_size(name):
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
        expected = config.known_size(name)
        if expected and part.stat().st_size != expected:
            raise IOError(f"{name} 大小不符（{part.stat().st_size} != {expected}），请重试")
        part.replace(dest_dir / name)
        done_before += have
        progress(done_before, total, name)
