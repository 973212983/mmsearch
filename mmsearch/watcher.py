"""监听已添加文件夹的变化（ReadDirectoryChangesW），合并成「要重新扫描的目录」交给索引器。

每个根文件夹一个线程，同步阻塞地读变化记录；停止时 CancelIoEx 打断阻塞的读取。
事件先攒着，安静 QUIET 秒（或最多攒 MAX_WAIT 秒）后一起回调，下载、解压、批量复制时不会每个文件跑一轮。
回调参数是 {(目录, 是否递归)}；None 表示记录溢出（变化太多）或出错，需要完整扫描一遍。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import struct
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from . import config

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k32.ReadDirectoryChangesW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.BOOL, wt.DWORD,
                                      ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.c_void_p]
k32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.CloseHandle.argtypes = [wt.HANDLE]

FILE_LIST_DIRECTORY = 0x0001
SHARE_ALL = 0x1 | 0x2 | 0x4  # 读、写、删除：不妨碍别的程序改名/删除这个文件夹
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # 打开目录必须带
INVALID_HANDLE = wt.HANDLE(-1).value
NOTIFY = 0x1 | 0x2 | 0x8 | 0x10  # 文件名、目录名、大小、最后写入时间
ADDED, REMOVED, MODIFIED, RENAMED_OLD, RENAMED_NEW = 1, 2, 3, 4, 5
BUF_SIZE = 64 * 1024  # 网络共享上限也是 64 KB

QUIET = 2.0
MAX_WAIT = 10.0

Scope = tuple[str, bool]


def _ignored(rel: str) -> bool:
    """和 indexer.scan 的跳过规则一致：隐藏目录、.git、node_modules 等里面的变化不处理。"""
    parts = rel.split(os.sep)
    return any(p in config.SKIP_DIRS or p.startswith(".") for p in parts[:-1])


def scopes_for(root: str, action: int, rel: str) -> list[Scope]:
    """一条变化记录 → 需要重新扫描的目录。
    支持的文件：重扫它所在的目录（不递归），新增、修改、删除、改名都能对上。
    其他路径可能是目录：新增/改名进来的递归扫描；删除/改名走的也「递归扫描」它，扫不到东西，
    原来记录在它下面的文件就会被当作已删除清理掉。目录自身的「修改」只是里面有变化，由子项的记录处理。"""
    if _ignored(rel):
        return []
    path = os.path.join(root, rel)
    name = os.path.basename(rel)
    if config.modality_of(Path(name)):
        return [] if name.startswith("~$") else [(os.path.dirname(path), False)]
    if action == MODIFIED or name in config.SKIP_DIRS or name.startswith("."):
        return []
    if action in (ADDED, RENAMED_NEW) and not os.path.isdir(path):
        return []
    return [(path, True)]


class FolderWatcher:
    def __init__(self, on_change: Callable[[set[Scope] | None], None]):
        self.on_change = on_change
        self._handles: dict[str, int] = {}
        self._lock = threading.Lock()
        self._pending: set[Scope] | None = set()
        self._first = self._last = 0.0
        self._wake = threading.Event()
        self._stopped = False
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def set_folders(self, folders: list[str]):
        """开始监听新增的根文件夹，停止监听已移除的。"""
        with self._lock:
            want = set(folders)
            for root in list(self._handles):
                if root not in want:
                    self._close(root)
            for root in want - set(self._handles):
                h = k32.CreateFileW(root, FILE_LIST_DIRECTORY, SHARE_ALL, None, OPEN_EXISTING,
                                    FILE_FLAG_BACKUP_SEMANTICS, None)
                if h and h != INVALID_HANDLE:  # 文件夹不存在（移动硬盘没插）就不监听
                    self._handles[root] = h
                    threading.Thread(target=self._read_loop, args=(root, h), daemon=True).start()

    def stop(self):
        self._stopped = True
        self._wake.set()
        with self._lock:
            for root in list(self._handles):
                self._close(root)

    def _close(self, root: str):
        h = self._handles.pop(root)
        k32.CancelIoEx(h, None)  # 打断阻塞中的 ReadDirectoryChangesW，读线程随后关闭句柄

    def _read_loop(self, root: str, h: int):
        buf = ctypes.create_string_buffer(BUF_SIZE)
        got = wt.DWORD()
        try:
            while self._handles.get(root) == h:
                ok = k32.ReadDirectoryChangesW(h, buf, BUF_SIZE, True, NOTIFY, ctypes.byref(got), None, None)
                if self._handles.get(root) != h:
                    break
                if not ok:  # 文件夹被删除、网络断开等：完整扫描一次（会清掉这个文件夹下的记录），不再监听
                    self._add(None)
                    break
                if got.value == 0:  # 变化太多，缓冲区溢出：不知道具体是哪些，完整扫描
                    self._add(None)
                    continue
                raw, scopes, off = buf.raw[: got.value], [], 0
                while True:  # FILE_NOTIFY_INFORMATION 链表：下一条偏移、动作、文件名字节数、文件名（相对路径）
                    nxt, action, n = struct.unpack_from("<III", raw, off)
                    rel = raw[off + 12: off + 12 + n].decode("utf-16-le")
                    scopes += scopes_for(root, action, rel)
                    if not nxt:
                        break
                    off += nxt
                if scopes:
                    self._add(set(scopes))
        finally:
            with self._lock:
                if self._handles.get(root) == h:
                    self._handles.pop(root)
            k32.CloseHandle(h)

    def _add(self, scopes: set[Scope] | None):
        with self._lock:
            now = time.monotonic()
            if not self._first:
                self._first = now
            self._last = now
            if scopes is None or self._pending is None:
                self._pending = None
            else:
                self._pending |= scopes
        self._wake.set()

    def _flush_loop(self):
        while not self._stopped:
            self._wake.wait()
            with self._lock:
                if not self._first:
                    self._wake.clear()
                    continue
                now = time.monotonic()
                due = min(self._last + QUIET, self._first + MAX_WAIT)
                if now < due:
                    wait = due - now
                else:
                    pending, self._pending, self._first = self._pending, set(), 0.0
                    wait = None
            if wait is not None:
                time.sleep(wait)
                continue
            if not self._stopped:
                try:
                    self.on_change(pending)
                except Exception:
                    traceback.print_exc()
