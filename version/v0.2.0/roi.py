"""
交互式框选。

给「工作区」和「安全箱位置」共用。抽出来是因为这两个地方都要同一套
鼠标拖拽 + 缩放映射逻辑，而且 main.py 不好从 safebox 里 import 这种东西。

坐标一律**归一化成比例**再存，这样换分辨率 / 换窗口大小都不用重标。
"""

from __future__ import annotations

import cv2
import numpy as np


def select(
    frame: np.ndarray,
    hint: str = "",
    title: str = "select region",
    skip_hint: str = "ESC = skip (use default)",
) -> tuple[int, int, int, int] | None:
    """
    在 frame 上拖动鼠标框选。回车/空格确认，Esc 跳过。

    返回**原图像素坐标** (x, y, w, h)；跳过或框太小返回 None。

    注意：cv2.putText 只支持 ASCII，中文会渲染成乱码，所以提示语用英文。
    """
    h, w = frame.shape[:2]
    max_w, max_h = 1500, 820
    scale = min(max_w / w, max_h / h, 1.0)
    disp = (cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if scale < 1.0 else frame.copy())

    st = {"p0": None, "p1": None, "drag": False}

    def on_mouse(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            st["p0"] = st["p1"] = (x, y)
            st["drag"] = True
        elif event == cv2.EVENT_MOUSEMOVE and st["drag"]:
            st["p1"] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP:
            st["p1"] = (x, y)
            st["drag"] = False

    win = title
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(win, on_mouse)

    try:
        while True:
            img = disp.copy()
            if st["p0"] and st["p1"]:
                cv2.rectangle(img, st["p0"], st["p1"], (0, 255, 255), 2)
                x0, y0 = st["p0"]
                x1, y1 = st["p1"]
                cv2.putText(img, f"{abs(x1-x0)}x{abs(y1-y0)}",
                            (min(x0, x1), max(min(y0, y1) - 6, 14)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            cv2.putText(img, hint, (14, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(img, skip_hint, (14, 56),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (150, 150, 150), 1)
            cv2.imshow(win, img)

            k = cv2.waitKey(20) & 0xFF
            if k == 27:
                return None
            if k in (13, 32) and st["p0"] and st["p1"]:
                break
    finally:
        cv2.destroyWindow(win)
        cv2.waitKey(1)          # Windows 上不等一下窗口不会真的关掉

    (ax, ay), (bx, by) = st["p0"], st["p1"]
    x0, x1 = sorted((ax, bx))
    y0, y1 = sorted((ay, by))
    X, Y = int(x0 / scale), int(y0 / scale)
    W, H = int((x1 - x0) / scale), int((y1 - y0) / scale)
    if W < 12 or H < 12:
        return None
    return (X, Y, W, H)


def to_normalized(rect: tuple[int, int, int, int],
                  frame_shape) -> tuple[float, float, float, float]:
    """(x,y,w,h) 像素 -> (fx,fy,fw,fh) 比例。"""
    h, w = frame_shape[:2]
    x, y, rw, rh = rect
    return (round(x / w, 4), round(y / h, 4), round(rw / w, 4), round(rh / h, 4))


def to_pixels(norm: tuple[float, float, float, float],
              frame_shape) -> tuple[int, int, int, int]:
    """(fx,fy,fw,fh) 比例 -> (x,y,w,h) 像素。"""
    h, w = frame_shape[:2]
    fx, fy, fw, fh = norm
    return (int(round(fx * w)), int(round(fy * h)),
            max(int(round(fw * w)), 1), max(int(round(fh * h)), 1))
