"""桌面外壳：pywebview（WebView2）+ 暴露给前端的 Api + 事件推送。"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import winreg
from pathlib import Path

import numpy as np
import webview

from . import config, media, shell
from .downloader import DownloadCancelled, download_models
from .embedder import EmbedError, Embedder, LlamaServer, hold_background
from .indexer import Indexer
from .mediaserver import MediaServer
from .store import Hit, VectorStore

WEB_DIR = config.RESOURCE_DIR / "mmsearch" / "web" if config.FROZEN else Path(__file__).parent / "web"
FILTERS = {"all": None, "text": {"text"}, "image": {"image"}, "audio": {"audio"}, "video": {"video", "video_audio"}}
TOP_K = 120
PLAYABLE = {".mp4", ".m4v", ".webm", ".mov", ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus"}


def system_dark() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def find_vlc() -> str | None:
    return shutil.which("vlc") or next((p for p in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                                                    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe")
                                        if os.path.exists(p)), None)


class Api:
    """前端通过 window.pywebview.api.* 调用。下划线开头的成员不会暴露给 JS。"""

    def __init__(self):
        self._settings = config.load_settings()
        src = self._settings.get("model_source")
        if not isinstance(src, dict) or config.check_model_source(src) or src == config.OFFICIAL_SOURCE:
            src = self._settings["model_source"] = {"kind": "official"}  # 被手改坏了，或旧版写入的展开形式
        config.set_model_source(src)
        self._store = VectorStore()
        self._media = MediaServer(self._store)
        self._server = LlamaServer(self._settings["backend"], self._settings["image_tokens"])
        self._emb = Embedder(self._server)
        self._window: webview.Window | None = None
        self._model = {"state": "idle", "device": "", "error": ""}
        self._model_gen = 0  # 每次启动模型 +1，过期的启动线程不再回写状态
        self._indexer: Indexer | None = None
        self._index_thread: threading.Thread | None = None
        self._index_state = {"running": False, "done": 0, "total": 0, "current": ""}
        self._reindex = False  # 索引进行中（或模型未就绪）时添加了文件夹：结束后再跑一轮
        self._download_cancel = False
        self._last_vec: np.ndarray | None = None
        self._last_exclude: str | None = None
        self._last_index_emit = 0.0
        self._hwnd = 0
        self._visible = False
        self._page_ready = False  # 页面加载完成前 evaluate_js 会阻塞 ~20s，此前的事件直接丢弃（前端 boot() 会拉全量状态）
        self._shown_at = 0.0
        self._backdrop: dict = {}  # 缩小后的桌面截图（未模糊），模糊半径变化时不必重新截屏
        self._pinned = False
        self._dialog_open = False
        self._quitting = False
        self._hotkey = shell.Hotkey(self.toggle)
        self._tray: shell.Tray | None = None

    # ================================================================ 事件
    def _emit(self, kind: str, data=None):
        if self._window and self._page_ready:
            try:
                self._window.evaluate_js(f"window.__mmEvent && window.__mmEvent({json.dumps(kind)}, "
                                         f"{json.dumps(data, ensure_ascii=False)})")
            except Exception:
                pass

    def _toast(self, text: str, level: str = "info"):
        self._emit("toast", {"text": text, "level": level})

    # ================================================================ 启动
    def boot(self) -> dict:
        return {"version": config.APP_VERSION, "settings": self._settings, "missing": config.missing_models(),
                "hotkey_ok": bool(self._hotkey.current), "autostart": shell.get_autostart(),
                "model_sizes": config.MODEL_SIZES, "endpoints": config.HF_ENDPOINTS,
                "official_source": config.OFFICIAL_SOURCE,
                "token_presets": config.IMAGE_TOKEN_PRESETS, "data_dir": str(config.DATA_DIR),
                "bg_url": self._bg_url(), "glass_ok": shell.blur_ok(),
                "model": self._model, "index": self._index_state, **self.overview()}

    def overview(self) -> dict:
        s = self._store.stats()
        return {"folders": self._store.folders(), "stats": s["files"], "items": s["items"], "errors": s["errors"]}

    def start_model(self):
        if self._model["state"] == "loading":
            return
        if config.missing_models():
            self._model = {"state": "missing", "device": "", "error": ""}
            self._emit("model", self._model)
            return
        self._model = {"state": "loading", "device": "", "error": ""}
        self._emit("model", self._model)
        self._model_gen += 1
        gen = self._model_gen

        def work():
            try:
                t = time.time()
                self._server.ensure_started()
                self._emb.encode_query("warmup")  # Vulkan 首次编译 shader
                if gen != self._model_gen:
                    return
                self._model = {"state": "ready", "device": self._server.device, "error": "",
                               "secs": round(time.time() - t, 1)}
                self._emit("model", self._model)
                rebuilt = self._check_index_model()
                if self._settings["auto_index"] or self._reindex or rebuilt:
                    self.start_index()
            except Exception as e:
                if gen != self._model_gen:
                    return
                traceback.print_exc()
                self._model = {"state": "error", "device": "", "error": str(e), "log": str(self._server.log_path)}
                self._emit("model", self._model)

        threading.Thread(target=work, daemon=True).start()

    def _check_index_model(self) -> bool:
        """索引是用别的模型建的：旧向量和新模型的查询不在同一空间，清空后重建。返回是否清空了。"""
        fp = config.model_fingerprint()
        old = self._store.get_meta("model")
        self._store.set_meta("model", fp)
        if old is None or old == fp or not self._store.stats()["items"]:
            return False  # 旧版索引库没有记录：当作就是当前模型建的
        self._store.clear()
        self._last_vec = None
        self._emit("overview", self.overview())
        self._toast("已更换模型，正在用新模型重建索引")
        return True

    def set_model_source(self, src: dict) -> dict:
        """设置页 / 引导页更换模型来源。仓库来源缺文件时前端进入下载页；模型就绪后自动重建索引。"""
        kind = src.get("kind")
        src = {"kind": kind, **{k: str(src.get(k) or "").strip() for k in ("repo", "model", "mmproj")}}
        if kind == "official":
            src = {"kind": "official"}
        elif kind == "local":
            src.pop("repo")
        err = config.check_model_source(src)
        if not err and kind == "local":
            err = next((f"文件不存在：{src[k]}" for k in ("model", "mmproj") if not Path(src[k]).is_file()), "")
        if err:
            return {"error": err}
        if src != self._settings.get("model_source"):
            self._settings["model_source"] = src
            config.save_settings(self._settings)
            config.set_model_source(src)
            self._restart_model()
        return {"source": src, "missing": config.missing_models()}

    def pick_model_file(self) -> str | None:
        self._dialog_open = True
        try:
            r = self._window.create_file_dialog(webview.FileDialog.OPEN,
                                                file_types=("GGUF 模型 (*.gguf)", "所有文件 (*.*)"))
        finally:
            self._dialog_open = False
        return str(r[0]) if r else None

    def _restart_model(self):
        self._model_gen += 1  # 先让进行中的启动线程失效，它被中断时不会报“启动失败”
        self._stop_index_and_wait()
        self._server.stop()  # 让进行中的启动放弃
        self._server.backend = self._settings["backend"]
        self._server.image_tokens = self._settings["image_tokens"]
        self._model = {"state": "idle", "device": "", "error": ""}
        self.start_model()

    def retry_model(self):
        self._restart_model()

    # ================================================================ 搜索
    def _hit(self, h: Hit) -> dict:
        p = Path(h.path)
        ext = p.suffix.lower()
        d = {"id": h.item_id, "path": h.path, "name": p.name, "dir": str(p.parent), "folder": p.parent.name,
             "kind": h.kind, "ext": ext.lstrip("."), "start": h.start, "end": h.end, "score": round(h.score, 4),
             "mtime": h.mtime}
        ms = self._media
        if h.kind == "text":
            d["snippet"] = h.snippet or ""
            d["code"] = ext in config.CODE_EXT
        elif h.kind == "image":
            d["icon"] = ms.url("thumb", h.path, w=96)
            d["thumb"] = ms.url("thumb", h.path, w=480)
            d["full"] = ms.url("thumb", h.path, w=1800)
        else:
            mid = ((h.start or 0) + (h.end or h.start or 0)) / 2
            if h.kind in ("video", "video_audio"):
                d["icon"] = ms.url("thumb", h.path, t=f"{mid:.2f}", w=96)
                d["thumb"] = ms.url("thumb", h.path, t=f"{mid:.2f}", w=480)
                d["poster"] = ms.url("thumb", h.path, t=f"{h.start or 0:.2f}", w=1280)
            if h.kind in ("audio", "video_audio"):
                d["wave"] = ms.url("wave", h.path, s=h.start, e=h.end, n=56)
            d["media"] = ms.url("file", h.path)
            d["playable"] = ext in PLAYABLE
        return d

    def _search_vec(self, vec: np.ndarray, t_enc: float = 0.0, exclude: str | None = None) -> dict:
        """exclude：以已索引文件搜索时排除它自己，否则第一个结果永远是查询本身。"""
        self._last_vec, self._last_exclude = vec, exclude
        t = time.time()
        res = {k: [self._hit(h) for h in self._store.search(vec, k=TOP_K, kinds=kinds, exclude_path=exclude)]
               for k, kinds in FILTERS.items()}
        return {"results": res, "enc_ms": round(t_enc * 1000), "search_ms": round((time.time() - t) * 1000, 1),
                "total": len(self._store)}

    def _need_model(self):
        if self._model["state"] != "ready":
            raise EmbedError({"loading": "模型正在加载，请稍候", "error": "模型启动失败"}.get(self._model["state"], "模型未就绪"))

    def _wrap(self, fn):
        try:
            return fn()
        except Exception as e:
            traceback.print_exc()
            return {"error": str(e)}

    def search_text(self, text: str) -> dict:
        def run():
            self._need_model()
            t = time.time()
            vec = self._emb.encode_query(text.strip())
            return self._search_vec(vec, time.time() - t)
        return self._wrap(run)

    def search_file(self, path: str) -> dict:
        def run():
            self._need_model()
            p = Path(path)
            mod = config.modality_of(p)
            if mod is None:
                raise ValueError(f"不支持用 {p.suffix or '这种'} 文件搜索")
            self._media.allow(path)
            t = time.time()
            if mod == "image":
                vec = self._emb.encode_images([media.load_image(p)])[0]
            elif mod == "audio":
                segs = media.split_audio(media.load_audio(p, max_sec=config.AUDIO_SEGMENT_SEC))
                if not segs:
                    raise ValueError("音频为空")
                vec = self._emb.encode_audios([segs[0][2]])[0]
            elif mod == "video":
                with contextlib.closing(media.video_segments(p)) as segs:  # 只抽第一段，不必把整个视频抽一遍
                    first = next(segs, None)
                if first is None:
                    raise ValueError("无法读取视频画面")
                vec = self._emb.encode_videos([first[2]])[0]
            else:
                chunks = media.chunk_text(media.read_text(p))
                if not chunks:
                    raise ValueError("文件没有文本内容")
                vec = self._emb.encode_documents(chunks[:1], title=p.name)[0]
            r = self._search_vec(vec, time.time() - t, exclude=path)
            r["query"] = {"kind": mod, "name": p.name, "path": path,
                          "thumb": self._media.url("thumb", path, w=160) if mod in ("image", "video") else None}
            return r
        return self._wrap(run)

    def search_image_data(self, data_url: str) -> dict:
        """粘贴/拖入的图片（data URL）。"""
        def run():
            self._need_model()
            import base64
            import io
            from PIL import Image
            raw = base64.b64decode(data_url.split(",", 1)[1])
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            t = time.time()
            vec = self._emb.encode_images([img])[0]
            return self._search_vec(vec, time.time() - t)
        return self._wrap(run)

    def search_similar(self, item_id: int) -> dict:
        def run():
            vec = self._store.vector(int(item_id))
            if vec is None:
                raise ValueError("该条目已不在索引中")
            return self._search_vec(vec, exclude=self._store.item_path(int(item_id)))
        return self._wrap(run)

    def rerun(self) -> dict | None:
        """索引更新后用上一次的查询向量重新检索（不需要重新编码）。"""
        if self._last_vec is None:
            return None
        return self._search_vec(self._last_vec, exclude=self._last_exclude)

    def pick_query_file(self) -> dict | None:
        exts = sorted(config.IMAGE_EXT | config.AUDIO_EXT | config.VIDEO_EXT | config.TEXT_EXT | config.DOC_EXT)
        self._dialog_open = True
        try:
            r = self._window.create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("支持的文件 (" + ";".join(f"*{e}" for e in exts) + ")", "所有文件 (*.*)"))
        finally:
            self._dialog_open = False
        if not r:
            return None
        p = Path(r[0])
        return {"path": str(p), "name": p.name, "kind": config.modality_of(p)}

    # ================================================================ 文件操作
    def open(self, path: str, start: float | None = None, kind: str = ""):
        vlc = find_vlc() if start is not None and kind != "text" else None
        if vlc:
            subprocess.Popen([vlc, f"--start-time={int(start)}", path])
        else:
            os.startfile(path)

    def reveal(self, path: str):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def copy_file(self, path: str):
        subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -LiteralPath $args[0]", path],
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        self._toast("已复制文件，可以在资源管理器里粘贴")

    def file_info(self, path: str) -> dict:
        try:
            st = os.stat(path)
            return {"exists": True, "size": st.st_size, "mtime": st.st_mtime}
        except OSError:
            return {"exists": False}

    def open_data_dir(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(config.DATA_DIR)

    # ================================================================ 文件夹 / 索引
    def folders_detail(self) -> dict:
        folders = [{"path": f, "exists": os.path.isdir(f), "counts": self._store.folder_counts(f)}
                   for f in self._store.folders()]
        fails = [{"path": p, "error": e} for p, e in self._store.failed_files()[:300]]
        return {"folders": folders, "failed": fails}

    def known_folders(self) -> list[dict]:
        """首次使用时一键添加的常用位置（文档、图片……），只返回存在的。"""
        added = {os.path.normcase(f) for f in self._store.folders()}
        out = []
        for key, label, guid in shell.KNOWN_FOLDERS:
            p = shell.known_folder(guid)
            if p and os.path.isdir(p):
                out.append({"key": key, "label": label, "path": p, "added": os.path.normcase(p) in added})
        return out

    def pick_folder(self) -> str | None:
        self._dialog_open = True
        try:
            r = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        finally:
            self._dialog_open = False
        if r:
            self.add_folder(r[0])
            return r[0]
        return None

    def add_folder(self, path: str):
        self.add_folders([path])

    def add_folders(self, paths: list[str]):
        for p in paths:
            if os.path.isdir(p):
                self._store.add_folder(os.path.normpath(p))
        self._emit("overview", self.overview())
        self.start_index()

    def remove_folder(self, path: str):
        running = self._index_state["running"]
        if running:  # 正在处理的文件可能属于这个文件夹，停下来，移除后再继续
            self._stop_index_and_wait()
        self._store.remove_folder(path)
        self._emit("overview", self.overview())
        if running:
            self.start_index()

    def retry_failed(self):
        self._store.retry_failed()
        self.start_index()

    def start_index(self):
        if not self._store.folders():
            return
        if self._index_state["running"] or self._model["state"] != "ready":
            self._reindex = True  # 当前一轮结束（或模型就绪）后再扫描一次，新加的文件夹不会被漏掉
            return
        self._reindex = False  # 新的一轮会完整扫描所有文件夹
        self._indexer = Indexer(self._store, Embedder(self._server))  # 独立实例：停止索引不影响搜索
        self._index_state = {"running": True, "done": 0, "total": 0, "current": "正在扫描文件…"}
        self._emit("index", self._index_state)

        def progress(done, total, msg):
            self._index_state = {"running": True, "done": done, "total": total,
                                 "current": Path(msg).name if os.sep in msg else msg}
            if time.time() - self._last_index_emit > 0.25 or done == total:
                self._last_index_emit = time.time()
                self._emit("index", self._index_state)
                self._emit("overview", self.overview())

        def work():
            err = None
            try:
                self._indexer.run(progress)
            except Exception as e:
                traceback.print_exc()
                err = str(e)
            cancelled = self._indexer.cancelled
            self._index_state = {"running": False, "done": 0, "total": 0, "current": ""}
            self._emit("index", self._index_state)
            self._emit("overview", self.overview())
            self._emit("index_done", {"error": err, "cancelled": cancelled, "failed": len(self._store.failed_files())})
            if self._reindex and not cancelled and not err:
                self.start_index()

        self._index_thread = threading.Thread(target=work, daemon=True)
        self._index_thread.start()

    def stop_index(self):
        if self._indexer:
            self._indexer.cancelled = True

    def _stop_index_and_wait(self):
        self.stop_index()
        if self._index_thread:
            self._index_thread.join(20)

    def clear_index(self, which: str):
        self._stop_index_and_wait()
        self._store.clear({"image", "video"} if which == "visual" else None)
        self._last_vec = None
        self._emit("overview", self.overview())
        self.start_index()

    # ================================================================ 设置
    def _settings_payload(self) -> dict:
        """设置改完后发给前端的全量状态（bg_url 每次重新生成：换图后要绕过缓存）。"""
        return {**self._settings, "autostart": shell.get_autostart(), "bg_url": self._bg_url()}

    def _bg_path(self) -> Path | None:
        name = self._settings.get("bg_image") or ""
        p = config.BG_DIR / name if name else None
        return p if p and p.is_file() else None

    def _bg_url(self) -> str:
        p = self._bg_path()
        if not p:
            return ""
        self._media.allow(str(p))
        return self._media.url("file", str(p), v=p.stat().st_mtime_ns)

    def pick_background(self) -> dict:
        """选一张图片当背景：统一转成 JPEG 存进数据目录（原图之后移动/删除都不影响）。"""
        from PIL import Image, ImageOps
        exts = sorted(config.IMAGE_EXT)
        self._dialog_open = True
        try:
            r = self._window.create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("图片 (" + ";".join(f"*{e}" for e in exts) + ")", "所有文件 (*.*)"))
        finally:
            self._dialog_open = False
        if not r:
            return {"cancel": True}
        try:
            config.BG_DIR.mkdir(parents=True, exist_ok=True)
            dst = config.BG_DIR / "background.jpg"
            with Image.open(r[0]) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((config.BG_MAX_SIDE, config.BG_MAX_SIDE), Image.LANCZOS)
                im.save(dst, "JPEG", quality=90, optimize=True)
            for old in config.BG_DIR.iterdir():  # 清掉上一次的图，避免数据目录里越堆越多
                if old != dst:
                    old.unlink(missing_ok=True)
        except Exception as e:
            traceback.print_exc()
            return {"error": f"读不了这张图片：{e}"}
        self._settings["bg_image"] = dst.name
        config.save_settings(self._settings)
        return self._settings_payload()

    def clear_background(self) -> dict:
        self._settings["bg_image"] = ""
        config.save_settings(self._settings)
        try:
            for old in config.BG_DIR.iterdir():
                old.unlink(missing_ok=True)
        except OSError:
            pass
        return self._settings_payload()

    def apply_native(self) -> dict:
        """把主题/磨砂重新应用到原生窗口（切换主题、系统主题变化、面板唤出时都调一次）。"""
        self._apply_native()
        return {"dark": self._dark(), "glass_ok": shell.blur_ok()}

    def _dark(self) -> bool:
        theme = self._settings.get("theme", "system")
        return system_dark() if theme == "system" else theme == "dark"

    def _apply_native(self):
        if self._hwnd:
            shell.style_window(self._hwnd, self._dark(), bool(self._settings.get("glass", True)))

    def _refresh_backdrop(self):
        # 首次总是留一张：若应用启动时关闭了磨砂，设置页重新开启也能立即生效。
        # 系统亚克力可用时不需要截图（亚克力由 DWM 实时合成，没有截屏开销也不会滞后）
        refresh = not shell.blur_ok() and (not self._backdrop or (
            self._settings.get("glass", True) and not self._settings.get("bg_image")))
        if refresh and self._hwnd:
            backdrop = shell.capture_backdrop()
            if backdrop:  # 临时截屏失败时保留上一张，不让界面退回 WebView2 的灰底
                self._backdrop = backdrop
        self._emit_backdrop()

    def _emit_backdrop(self):
        if shell.blur_ok():
            self._emit("backdrop", {})  # 前端撤掉截图层，让亚克力透出来
        elif self._backdrop:
            self._emit("backdrop", shell.blur_backdrop(self._backdrop, float(self._settings.get("bg_blur", 18))))

    def save_settings(self, new: dict) -> dict:
        """设置页每改一项就调用一次（可以只传改动的键），立即生效并持久化。"""
        restart = any(k in new and new[k] != self._settings.get(k) for k in ("backend", "image_tokens"))
        native = any(k in new and new[k] != self._settings.get(k) for k in ("theme", "glass"))
        if new.get("hotkey") and new["hotkey"] != self._hotkey.current:
            if not self.set_hotkey(new["hotkey"]):
                new["hotkey"] = self._settings["hotkey"]
        if "autostart" in new:
            shell.set_autostart(bool(new.pop("autostart")))
        self._settings.update({k: new[k] for k in config.DEFAULT_SETTINGS if k in new})
        config.save_settings(self._settings)
        if native:
            self._apply_native()
        if "bg_blur" in new:
            self._emit_backdrop()  # 磨砂底图的模糊是预先做好的，半径变了要重新生成
        if restart:
            self._restart_model()
        return self._settings_payload()

    # ================================================================ 面板显示
    def show(self, view: str | None = None):
        if not self._hwnd:
            return
        self._apply_native()  # 系统主题变化时 pywebview 会改掉窗口材质，唤出时重新确认一次
        hold_background(8)  # 唤出面板多半是要搜索：先别让索引占住推理，第一个查询就不用等图片编码
        shell.move_to_cursor_screen(self._hwnd)
        if not self._visible:
            self._refresh_backdrop()  # 必须在 show 前截屏，避免把面板自己拍进磨砂底图
        self._window.show()
        self._apply_native()  # 隐藏时设的亚克力在显示后可能不生效（打包版实测一直灰底），显示后再设一次
        shell.force_foreground(self._hwnd)
        self._visible = True
        self._shown_at = time.time()
        self._emit("shown", {"view": view})

    def hide(self):
        if self._visible:
            self._visible = False
            self._window.hide()
            self._emit("hidden")

    def toggle(self):
        if self._visible and ctypes.windll.user32.GetForegroundWindow() == self._hwnd:
            self.hide()
        else:
            self.show()

    def on_blur(self):
        """失焦自动收起（可在设置关闭；固定、对话框、刚显示时除外）。"""
        if (self._settings.get("autohide") and not self._pinned and not self._dialog_open
                and time.time() - self._shown_at > 0.5):
            self.hide()

    def set_pinned(self, pinned: bool):
        self._pinned = bool(pinned)

    def set_hotkey(self, combo: str) -> bool:
        old = self._hotkey.current or self._settings.get("hotkey")
        if self._hotkey.register(combo):
            return True
        if old:
            self._hotkey.register(old)
        self._toast(f"快捷键 {combo} 已被其他程序占用，请换一个", "error")
        return False

    def quit(self):
        self._quitting = True
        if self._tray:
            self._tray.stop()
        self._hotkey.unregister()
        self._window.destroy()

    def _on_closing(self):
        """点关闭 / Alt+F4 只是收起到托盘。"""
        if self._quitting:
            self._shutdown()
            return True
        self.hide()
        return False

    # ================================================================ 下载模型
    def start_download(self, endpoint: str):
        self._settings["hf_endpoint"] = endpoint
        config.save_settings(self._settings)
        self._download_cancel = False
        t0 = [time.time(), None]

        def progress(done, total, name):
            if t0[1] is None:
                t0[0], t0[1] = time.time(), done
            dt = time.time() - t0[0]
            speed = (done - t0[1]) / dt if dt > 0.5 else 0
            self._emit("download", {"done": done, "total": total, "name": name, "speed": speed})

        def work():
            try:
                download_models(endpoint, progress, lambda: self._download_cancel)
                self._emit("download_done", {"missing": config.missing_models()})
                self.start_model()
            except DownloadCancelled:
                self._emit("download_failed", {"error": "", "paused": True})
            except Exception as e:
                traceback.print_exc()
                self._emit("download_failed", {"error": str(e)})

        threading.Thread(target=work, daemon=True).start()

    def cancel_download(self):
        self._download_cancel = True

    # ================================================================ 窗口
    def _on_drop(self, e):
        """WebView2 拖放：pywebview 提供完整路径。"""
        try:
            files = e.get("dataTransfer", {}).get("files", [])
            paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
            if not paths:
                return
            p = paths[0]
            if os.path.isdir(p):
                for d in paths:
                    if os.path.isdir(d):
                        self.add_folder(d)
                self._toast(f"已添加 {Path(p).name}，正在建立索引")
            else:
                self._emit("drop_file", {"path": p, "name": Path(p).name, "kind": config.modality_of(Path(p))})
        except Exception:
            traceback.print_exc()

    def _shutdown(self):
        self.stop_index()
        self._server.stop()
        self._media.httpd.shutdown()


SINGLE_INSTANCE_MUTEX = f"Local\\{config.APP_NAME}-single-instance"


_MUTEX = None
SHOW_EVENT = f"Local\\{config.APP_NAME}-show"


def activate_existing() -> bool:
    """已有实例时通知它显示面板并返回 True。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    global _MUTEX  # 句柄保持到进程结束
    k32.CreateMutexW.restype = ctypes.c_void_p
    _MUTEX = k32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return False
    k32.OpenEventW.restype = ctypes.c_void_p
    ev = k32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
    if ev:
        k32.SetEvent(ctypes.c_void_p(ev))
        k32.CloseHandle(ctypes.c_void_p(ev))
    return True


def listen_show_requests(api: "Api"):
    """后启动的实例通过命名事件请求显示面板。"""
    k32 = ctypes.windll.kernel32
    k32.CreateEventW.restype = ctypes.c_void_p
    ev = k32.CreateEventW(None, False, False, SHOW_EVENT)

    def loop():
        while k32.WaitForSingleObject(ctypes.c_void_p(ev), ctypes.c_uint32(0xFFFFFFFF)) == 0:  # INFINITE
            api.show()

    threading.Thread(target=loop, daemon=True).start()


PANEL_W, PANEL_H = 900, 600


class _Timing:
    """MMSEARCH_TIMING=1 时把启动各阶段耗时追加到日志（诊断打包后启动慢用）。"""

    def __init__(self):
        self.enabled = bool(os.environ.get("MMSEARCH_TIMING"))
        self.t0 = getattr(config, "START_T0", time.perf_counter())

    def mark(self, label: str):
        if not self.enabled:
            return
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(config.DATA_DIR / "startup-timing.log", "a", encoding="utf-8") as f:
                f.write(f"[{time.perf_counter() - self.t0:8.2f}s] {label}\n")
        except OSError:
            pass


_timing = _Timing()


def _clear_form_background(form):
    """pywebview 只把 WebView2 设成透明，WinForms 窗体仍画灰色 BackColor，透明像素透出的就是这层灰。
    改成黑色：DWM 边框扩展到客户区后 GDI 黑色按透明处理，亚克力才能透出来。"""
    from System import Action
    from System.Drawing import Color

    def apply():
        form.BackColor = Color.Black

    try:
        form.Invoke(Action(apply)) if form.InvokeRequired else apply()
    except Exception:
        traceback.print_exc()


def main():
    if activate_existing():
        return 0
    _timing.mark("main entry")
    start_hidden = "--hidden" in sys.argv
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True  # 只有空白处可拖动窗口
    _timing.mark("pre-Api")
    api = Api()
    _timing.mark("Api() done")
    theme = api._settings.get("theme", "system")
    dark = system_dark() if theme == "system" else theme == "dark"
    # x/y：透明窗口在页面开始加载时会被 pywebview 自己 Show 出来，后台启动时先待在屏幕外，
    # 等 Api.show() 再移到光标所在屏幕（否则自启动会看到一个一闪而过的空面板）
    offscreen = -32000 if start_hidden else None
    window = webview.create_window(
        config.APP_NAME, url=str(WEB_DIR / "index.html"), js_api=api, width=PANEL_W, height=PANEL_H,
        frameless=True, easy_drag=False, resizable=False, hidden=True, shadow=True, transparent=True,
        x=offscreen, y=offscreen,
        background_color="#1e2024" if dark else "#fbfbfc", text_select=False)
    api._window = window
    _timing.mark("create_window done")

    def on_loaded():
        _timing.mark("event: loaded")
        api._page_ready = True
        try:
            from webview.dom import DOMEventHandler
            window.dom.document.events.drop += DOMEventHandler(api._on_drop, True, True)
        except Exception:
            traceback.print_exc()
        if not api._hwnd:
            _clear_form_background(window.native)
            api._hwnd = window.native.Handle.ToInt64()
            api._apply_native()
            if start_hidden:
                window.hide()  # 加载完成，屏幕外的那份现在就真正隐藏（不出现在任务栏）
            else:
                api.show()

    def on_ready():
        _timing.mark("event: ready")
        if not api._hotkey.register(api._settings.get("hotkey", "alt+space")):
            api._toast(f"全局快捷键 {api._settings.get('hotkey')} 注册失败，可在设置中更换", "error")
        label = lambda: (api._hotkey.current or "").replace("+", " + ").title()
        api._tray = shell.Tray(lambda: api.toggle(), lambda: api.show("index"), lambda: api.show("settings"),
                               lambda: api.quit(), label)
        if start_hidden and label():
            api._tray.notify(f"MMSearch 已在后台运行，按 {label()} 打开")

    window.events.loaded += on_loaded
    window.events.closing += api._on_closing
    _timing.mark("events wired")
    listen_show_requests(api)
    _timing.mark("show listener")
    api.start_model()
    _timing.mark("start_model called")
    storage = config.DATA_DIR / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    _timing.mark("pre webview.start")
    try:
        webview.start(on_ready, gui="edgechromium", private_mode=False, storage_path=str(storage),
                      debug=bool(os.environ.get("MMSEARCH_DEBUG")))
    except Exception as e:
        ctypes.windll.user32.MessageBoxW(
            None, f"无法启动界面（需要 Microsoft Edge WebView2 运行时）。\n\n{e}\n\n"
                  "可从 https://go.microsoft.com/fwlink/p/?LinkId=2124703 安装。", config.APP_NAME, 0x10)
        return 1
    _timing.mark("webview.start returned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
