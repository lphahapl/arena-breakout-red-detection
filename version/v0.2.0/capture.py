"""
窗口定位 + 截图。

坐标锚定在「游戏窗口客户区」而不是显示器，这样窗口移动、多显示器、
窗口化 / 无边框全屏都不用改代码。

只用 ctypes 调 user32，不依赖 pywin32。
"""

from __future__ import annotations

import ctypes
import json
from ctypes import wintypes
from pathlib import Path

import numpy as np

try:
    import mss
except ImportError:  # pragma: no cover
    raise SystemExit("缺少依赖，先执行:  python -m pip install mss")


# --------------------------------------------------------------------------
# DPI 感知
# 必须在任何截图 / 取窗口坐标之前设置。缩放不是 100% 时如果不设，
# 拿到的 client 尺寸和截到的像素尺寸会对不上，后面所有比例全错。
# --------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_user32 = ctypes.WinDLL("user32", use_last_error=True)

_ENUM_CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


# --------------------------------------------------------------------------
# 窗口枚举
# --------------------------------------------------------------------------
def _title_of(hwnd: int) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _client_size(hwnd: int) -> tuple[int, int]:
    r = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(r)):
        return 0, 0
    return r.right - r.left, r.bottom - r.top


def list_windows(min_side: int = 200) -> list[dict]:
    """列出所有可见的、客户区大于 min_side 的顶层窗口。"""
    out: list[dict] = []

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        title = _title_of(hwnd)
        if not title:
            return True
        w, h = _client_size(hwnd)
        if w < min_side or h < min_side:
            return True
        out.append({"hwnd": hwnd, "title": title, "w": w, "h": h})
        return True

    _user32.EnumWindows(_ENUM_CB(_cb), 0)
    return out


def find_window(substr: str) -> dict | None:
    """按标题子串（不区分大小写）找窗口。substr 为空返回 None。"""
    if not substr:
        return None
    needle = substr.lower()
    for w in list_windows():
        if needle in w["title"].lower():
            return w
    return None


def foreground_window() -> dict | None:
    """当前前台窗口。找不到游戏窗口时的兜底 —— 需要游戏保持在最前面。"""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    w, h = _client_size(hwnd)
    if w < 200 or h < 200:
        return None
    return {"hwnd": hwnd, "title": _title_of(hwnd), "w": w, "h": h}


SETTINGS = Path(__file__).resolve().parent / "settings.json"


def load_settings() -> dict:
    """读 settings.json。没有或坏了都返回空 dict，不抛异常。"""
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(d: dict) -> None:
    SETTINGS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_window_interactive(min_side: int = 400) -> str:
    """
    列出可见窗口让用户输入编号，返回标题。

    为什么不直接让用户敲中文标题：cmd.exe 解析 .bat 和读输入用的是控制台
    代码页，中文很容易乱码。输入数字就没有这个问题。
    """
    ws = sorted(list_windows(min_side=min_side), key=lambda x: -(x["w"] * x["h"]))
    if not ws:
        return ""
    print("\n检测到以下窗口（按面积从大到小）:")
    for i, w in enumerate(ws[:15]):
        print(f"  [{i:2d}] {w['title']}   ({w['w']}x{w['h']})")
    print("\n  输入游戏窗口的编号，直接回车 = 0")
    try:
        s = input("  编号: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    idx = int(s) if s.isdigit() else 0
    if not (0 <= idx < min(len(ws), 15)):
        return ""
    return ws[idx]["title"]


def resolve_window(cli_value: str = "") -> str:
    """定抓取目标：命令行 > settings.json > 交互式选择。"""
    if cli_value:
        return cli_value
    saved = load_settings().get("window", "")
    if saved and find_window(saved):
        return saved
    return pick_window_interactive()


def is_foreground(hwnd: int) -> bool:
    """目标窗口是不是当前前台窗口。保留给别处用，判定可见性请用 window_visible_ratio。"""
    return _user32.GetForegroundWindow() == hwnd


def window_visible_ratio(hwnd: int, rect: dict, grid: int = 5) -> float:
    """
    窗口矩形内有多少比例的采样点**真的属于这个窗口**（没被别的窗口盖住）。

    为什么不用 is_foreground：那是"在不在前台"，**多屏下完全不适用** ——
    游戏放一块屏、浏览器放另一块屏，点一下浏览器游戏就不是"前台"了，
    但它根本没被挡住、抓屏完全正常。
    实测踩过：用户扩展屏幕后工具直接报"游戏不在前台"，什么都扫不到。

    真正要回答的是「有没有被别的窗口遮住」—— mss 抓的是桌面合成结果，
    被盖住时拿到的就是上面那个窗口的像素。用 WindowFromPoint 在窗口矩形里
    采样，每个点往上找顶层祖先，看是不是目标窗口。
    """
    if rect["width"] <= 0 or rect["height"] <= 0:
        return 0.0
    hit = total = 0
    for iy in range(grid):
        for ix in range(grid):
            x = rect["left"] + int(rect["width"] * (ix + 0.5) / grid)
            y = rect["top"] + int(rect["height"] * (iy + 0.5) / grid)
            h = _user32.WindowFromPoint(wintypes.POINT(x, y))
            total += 1
            if h and _user32.GetAncestor(h, 2) == hwnd:   # GA_ROOT = 2
                hit += 1
    return hit / total if total else 0.0


def client_rect_on_screen(hwnd: int) -> dict | None:
    """客户区在屏幕上的绝对像素矩形，直接喂给 mss。"""
    r = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:  # 最小化时是 0
        return None
    pt = wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return {"left": pt.x, "top": pt.y, "width": w, "height": h}


# --------------------------------------------------------------------------
# 抓图
# --------------------------------------------------------------------------
class Grabber:
    """按窗口客户区抓图。窗口位置每帧重新取，跟着窗口走。"""

    def __init__(self, window_substr: str | None = None, monitor: int = 1,
                 force_monitor: bool = False, allow_fallback: bool = True,
                 crop: tuple | None = None):
        """
        crop: (fx, fy, fw, fh) 归一化比例，只抓客户区的这一块。
        实测抓整个 2560x1440 要 29ms，只抓右侧 40% 只要 14ms —— **2.1 倍**。
        BitBlt 的成本主要跟面积走，而我们只需要工作区那一块。
        """
        self._sct = mss.mss()
        self._monitor = monitor
        self.window: dict | None = None
        self._fallback_rect: dict | None = None
        self._last_rect: dict | None = None
        self._crop = crop

        # allow_fallback=False 时**绝不**退回前台窗口。
        # 实测踩过：窗口标题写错时静默退回前台，而前台是浏览器 ——
        # 工具对着浏览器里的 B 站视频跑完了整条链路，OCR 和色块检测都"正常"，
        # 但数据全是错的。这种"看着合理的错数据"比直接报错危险得多。
        self.strict = not allow_fallback
        if not force_monitor:
            if window_substr:
                self.window = find_window(window_substr)
            if self.window is None and allow_fallback:
                self.window = foreground_window()

        if self.window is None and self.strict:
            # 不用 !r：repr 会把标题里的零宽空格之类转义成 ​ 直接暴露给用户
            self._sct.close()
            raise LookupError(f"找不到标题含「{window_substr}」的窗口")
        if self.window is None:
            # 退化到整块显示器。此时 0.6/0.4 的比例是相对显示器而非游戏窗口，
            # 只有在无边框全屏时才对得上。
            m = self._sct.monitors[self._monitor]
            self._fallback_rect = {
                "left": m["left"], "top": m["top"],
                "width": m["width"], "height": m["height"],
            }

    @property
    def target_name(self) -> str:
        if self.window:
            return f"{self.window['title']} (client {self.window['w']}x{self.window['h']})"
        return f"monitor#{self._monitor}"

    @property
    def using_fallback(self) -> bool:
        return self.window is None

    def region(self) -> dict:
        r = None
        if self.window:
            r = client_rect_on_screen(self.window["hwnd"])
            if r:
                self._last_rect = r
            elif self._last_rect:  # 窗口最小化的瞬间，用上一帧的
                r = self._last_rect
        if r is None:
            assert self._fallback_rect is not None
            r = self._fallback_rect
        if self._crop:
            fx, fy, fw, fh = self._crop
            return {"left": r["left"] + int(r["width"] * fx),
                    "top": r["top"] + int(r["height"] * fy),
                    "width": max(int(r["width"] * fw), 1),
                    "height": max(int(r["height"] * fh), 1)}
        return r

    def grab(self) -> np.ndarray:
        """返回 BGR uint8 数组。"""
        return np.asarray(self._sct.grab(self.region()))[:, :, :3].copy()

    def close(self) -> None:
        self._sct.close()
