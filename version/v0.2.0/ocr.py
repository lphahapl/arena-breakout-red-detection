"""
Windows 自带 OCR 的封装（走 WinRT，不经过 PowerShell）。

为什么不用 PowerShell：本机执行策略是 Restricted，.ps1 脚本文件被系统禁止运行，
要跑就得加 -ExecutionPolicy Bypass——那是在拆系统的安全开关，不该干。
WinRT 的 Python 投影包是正规入口，同一个引擎，没有这个问题。

依赖（都有 Python 3.13 的 wheel，不用编译）：
    pip install winrt-runtime winrt-Windows.Media.Ocr \
        winrt-Windows.Graphics.Imaging winrt-Windows.Storage \
        winrt-Windows.Storage.Streams winrt-Windows.Globalization \
        winrt-Windows.Foundation

本机已确认装了 zh-Hans-CN 语言包。
"""

from __future__ import annotations

import asyncio

import cv2
import numpy as np

try:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        f"缺少 WinRT OCR 依赖 ({e})。安装:\n"
        "  pip install winrt-runtime winrt-Windows.Media.Ocr "
        "winrt-Windows.Graphics.Imaging winrt-Windows.Storage "
        "winrt-Windows.Storage.Streams winrt-Windows.Globalization winrt-Windows.Foundation"
    )

DEFAULT_LANG = "zh-Hans-CN"

_engine_cache: dict[str, object] = {}


def available_languages() -> list[str]:
    return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]


def _get_engine(lang_tag: str):
    if lang_tag not in _engine_cache:
        eng = OcrEngine.try_create_from_language(Language(lang_tag))
        if eng is None:
            raise RuntimeError(
                f"无法创建 {lang_tag} 的 OCR 引擎。已安装语言: {available_languages()}")
        _engine_cache[lang_tag] = eng
    return _engine_cache[lang_tag]


def _to_png_bytes(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("imencode 失败")
    return buf.tobytes()


async def _recognize_async(png: bytes, lang_tag: str):
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    return await _get_engine(lang_tag).recognize_async(bitmap)


def recognize(bgr: np.ndarray, lang_tag: str = DEFAULT_LANG,
              upscale: float = 1.0) -> list[dict]:
    """
    识别 BGR 图里的文字。

    upscale > 1 会先放大再识别 —— Windows OCR 对小字识别率一般，放大能明显改善。

    返回 [{"text": str, "bbox": (x, y, w, h), "words": [...]}, ...]
    坐标已还原到**原图**尺度（放大过的话会自动除回去）。
    """
    if upscale != 1.0:
        img = cv2.resize(bgr, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    else:
        img = bgr

    result = asyncio.run(_recognize_async(_to_png_bytes(img), lang_tag))

    lines = []
    for line in result.lines:
        words = []
        xs, ys, xe, ye = [], [], [], []
        for w in line.words:
            r = w.bounding_rect
            words.append({"text": w.text,
                          "bbox": tuple(v / upscale for v in (r.x, r.y, r.width, r.height))})
            xs.append(r.x); ys.append(r.y)
            xe.append(r.x + r.width); ye.append(r.y + r.height)
        if not words:
            continue
        lines.append({
            "text": line.text,
            "bbox": (min(xs) / upscale, min(ys) / upscale,
                     (max(xe) - min(xs)) / upscale, (max(ye) - min(ys)) / upscale),
            "words": words,
        })
    return lines


def flatten(s: str) -> str:
    """
    去掉所有空白。

    Windows OCR 会在汉字之间插空格（'安全箱' -> '安 全 箱'），
    直接 `"安全箱" in "安 全 箱"` 是 False。所有文本比较前都必须先过这一步。
    """
    return "".join(s.split())


def find_keyword(bgr: np.ndarray, keyword: str, lang_tag: str = DEFAULT_LANG,
                 upscale: float = 1.0) -> list[dict]:
    """
    识别并返回**去空白后**包含 keyword 的行。

    用子串匹配，所以「钛金安全箱」「钥匙保险箱」都能被「安全箱」命中。
    返回的每项多一个 "flat" 字段是去空白后的文本，方便写日志。
    """
    if not keyword:
        return []
    hits = []
    for ln in recognize(bgr, lang_tag, upscale):
        flat = flatten(ln["text"])
        if keyword in flat:
            hits.append({**ln, "flat": flat})
    return hits
