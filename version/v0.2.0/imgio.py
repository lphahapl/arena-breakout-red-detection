"""
Unicode 安全的图片读写。

cv2.imwrite / cv2.imread 在 Windows 上遇到非 ASCII 路径会**静默失败**
（返回 False，不抛异常）。而本项目的目录是 C:\\Users\\鲁迅\\...，
所以必须绕开 OpenCV 的文件 IO，改用 imencode + Python 文件对象。

已验证：cv2.imwrite(r'C:/Users/鲁迅/x.png', img) -> False，文件不生成。
"""

from __future__ import annotations

import os

import cv2
import numpy as np


def imwrite_u(path, img) -> bool:
    """等价 cv2.imwrite，但支持任意 Unicode 路径。失败会抛异常而不是静默返回 False。"""
    path = str(path)
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise RuntimeError(f"imencode 失败: {path}")
    with open(path, "wb") as f:
        f.write(buf.tobytes())
    return True


def imread_u(path) -> np.ndarray | None:
    """
    等价 cv2.imread，但支持任意 Unicode 路径。

    和 cv2.imread 一样：文件不存在 / 读不了就返回 None，不抛异常。
    调用方靠 None 判断，所以这里必须吞掉 OSError。
    """
    try:
        with open(str(path), "rb") as f:
            data = np.frombuffer(f.read(), np.uint8)
    except OSError:
        return None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return img
