"""Windows 外壳能力：全局快捷键、托盘、窗口定位/圆角/前台、开机自启。"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import io
import sys
import threading
import winreg
from typing import Callable

from . import config

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
VKS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "`": 0xC0, ";": 0xBA, "/": 0xBF, ".": 0xBE}
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


def parse_hotkey(s: str) -> tuple[int, int] | None:
    """'alt+space' / 'ctrl+shift+f' → (modifiers, vk)。"""
    parts = [p.strip().lower() for p in s.split("+") if p.strip()]
    if len(parts) < 2:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    key = parts[-1]
    if key in VKS:
        vk = VKS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        vk = 0x70 + int(key[1:]) - 1
    else:
        return None
    return mods, vk


class Hotkey:
    """在独立线程里注册全局快捷键并跑消息循环。"""

    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self.thread: threading.Thread | None = None
        self.thread_id = 0
        self.current = ""

    def register(self, combo: str) -> bool:
        self.unregister()
        hk = parse_hotkey(combo)
        if not hk:
            return False
        ok = threading.Event()
        result = {"ok": False}

        def loop():
            self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            result["ok"] = bool(user32.RegisterHotKey(None, 1, hk[0] | MOD_NOREPEAT, hk[1]))
            ok.set()
            if not result["ok"]:
                return
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        pass
            user32.UnregisterHotKey(None, 1)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        ok.wait(2)
        self.current = combo if result["ok"] else ""
        return result["ok"]

    def unregister(self):
        if self.thread and self.thread.is_alive() and self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
            self.thread.join(2)
        self.thread = None
        self.current = ""


# ---------------------------------------------------------------- 窗口
class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def placement_for_cursor(w: int, h: int) -> tuple[int, int]:
    """光标所在屏幕：水平居中，顶部约 20% 处（物理像素）。"""
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    mon = user32.MonitorFromPoint(pt, 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    wa = mi.rcWork
    x = wa.left + (wa.right - wa.left - w) // 2
    y = wa.top + max(24, int((wa.bottom - wa.top) * 0.18))
    y = min(y, wa.bottom - h - 12)
    return x, y


def window_size(hwnd: int) -> tuple[int, int]:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def move_to_cursor_screen(hwnd: int):
    w, h = window_size(hwnd)
    x, y = placement_for_cursor(w, h)
    user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE


BACKDROP_SCALE = 4  # 磨砂底图缩小倍数：反正要模糊，小图放大后看不出差别


def capture_backdrop() -> dict | None:
    """截取整个虚拟桌面并缩小，作为 WebView2 透明失效时的磨砂底图原料。

    Evergreen WebView2 偶尔会回归为忽略透明背景、直接绘制灰色底。截图只存在内存中，
    每次从隐藏状态唤出时刷新；模糊在 blur_backdrop 里预先做好，前端只移动整张图的位置，
    不再跑 CSS blur（拖动时逐帧重新模糊大图是卡顿的主因）。
    """
    from PIL import ImageGrab

    left = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    top = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if width <= 0 or height <= 0:
        return None
    try:
        image = ImageGrab.grab(all_screens=True).convert("RGB").reduce(BACKDROP_SCALE)
        return {"image": image, "left": left, "top": top, "width": width, "height": height}
    except (OSError, ValueError):
        return None


def blur_backdrop(shot: dict, blur_px: float) -> dict:
    """把缩小的截图按设置的模糊半径（屏幕像素）模糊后编码成前端可用的 data URL。"""
    from PIL import ImageFilter

    image = shot["image"]
    if blur_px > 0:
        image = image.filter(ImageFilter.GaussianBlur(blur_px / BACKDROP_SCALE))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return {
        "url": "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii"),
        **{k: shot[k] for k in ("left", "top", "width", "height")},
    }


class MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_ulong)]


WCA_ACCENT_POLICY = 19
ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3  # 让系统模糊窗口背后的内容（Win10 1803+ 可用）
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
_BLUR_OK = True


DWMSBT_NONE = 1
DWMSBT_TRANSIENTWINDOW = 3  # 亚克力：DWM 实时模糊窗口背后的内容（Win11 22H2+）


def set_blur_behind(hwnd: int, on: bool) -> bool:
    """旧系统兜底：Win10 的 blur-behind（拖动时会拖影，只在亚克力不可用时用）。"""
    policy = ACCENTPOLICY(ACCENT_ENABLE_BLURBEHIND if on else ACCENT_DISABLED, 0, 0x00000000, 0)
    data = WINCOMPATTRDATA(WCA_ACCENT_POLICY, ctypes.cast(ctypes.byref(policy), ctypes.c_void_p),
                           ctypes.sizeof(policy))
    try:
        return bool(user32.SetWindowCompositionAttribute(ctypes.c_void_p(hwnd), ctypes.byref(data)))
    except (AttributeError, OSError):
        return False


def set_acrylic(hwnd: int, on: bool) -> bool:
    """系统亚克力材质。需要把 DWM 边框扩展到整个客户区，窗体底色为黑色（GDI 黑 = DWM 透明），
    WebView2 透明像素才会透出 DWM 画的亚克力，而不是窗体自己的灰底。"""
    margins = MARGINS(-1, -1, -1, -1) if on else MARGINS(0, 0, 0, 0)
    dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    kind = ctypes.c_int(DWMSBT_TRANSIENTWINDOW if on else DWMSBT_NONE)
    return dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(kind), 4) == 0


def blur_ok() -> bool:
    """上一次尝试系统亚克力的结果；False 时前端改用截图磨砂兜底。"""
    return _BLUR_OK


def style_window(hwnd: int, dark: bool, glass: bool = True):
    """Win11 圆角 + 深色边框 + 亚克力。旧系统上部分调用无效但无害。"""
    global _BLUR_OK
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), 4)
    flag = ctypes.c_int(1 if dark else 0)  # 亚克力的明暗也跟这个走
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(flag), 4)
    ok = set_acrylic(hwnd, glass)
    if glass:
        _BLUR_OK = ok
    set_blur_behind(hwnd, glass and not ok)


def force_foreground(hwnd: int):
    """从后台把窗口切到前台（绕过前台锁：附着到当前前台线程的输入队列）。"""
    fg = user32.GetForegroundWindow()
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    if fg_thread and fg_thread != cur:
        user32.AttachThreadInput(fg_thread, cur, True)
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    if fg_thread and fg_thread != cur:
        user32.AttachThreadInput(fg_thread, cur, False)


# ---------------------------------------------------------------- 托盘
class Tray:
    def __init__(self, on_toggle, on_index, on_settings, on_quit, hotkey_label: Callable[[], str]):
        import pystray
        from PIL import Image

        icon = Image.open(config.RESOURCE_DIR / "assets" / "mmsearch.png")
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: f"打开搜索（{hotkey_label()}）" if hotkey_label() else "打开搜索",
                             lambda: on_toggle(), default=True),
            pystray.MenuItem("索引管理", lambda: on_index()),
            pystray.MenuItem("设置", lambda: on_settings()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 MMSearch", lambda: on_quit()),
        )
        self.icon = pystray.Icon(config.APP_NAME, icon, config.APP_NAME, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def notify(self, text: str):
        try:
            self.icon.notify(text, config.APP_NAME)
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- 常用文件夹
KNOWN_FOLDERS = [  # (key, 显示名, FOLDERID)；用系统 API 取路径，OneDrive 重定向后也正确
    ("documents", "文档", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"),
    ("pictures", "图片", "{33E28130-4E1E-4676-835A-98395C3BC3BB}"),
    ("videos", "视频", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}"),
    ("music", "音乐", "{4BD8D571-6D19-48D3-BE97-422220080E43}"),
    ("desktop", "桌面", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"),
    ("downloads", "下载", "{374DE290-123F-4565-9164-39C4925E467B}"),
]


def known_folder(guid: str) -> str | None:
    import uuid
    rfid = ctypes.create_string_buffer(uuid.UUID(guid).bytes_le, 16)
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(rfid, 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


# ---------------------------------------------------------------- 开机自启
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    if config.FROZEN:
        return f'"{sys.executable}" --hidden'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{config.APP_DIR / "main.py"}" --hidden'


def get_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, config.APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, config.APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, config.APP_NAME)
            except OSError:
                pass
