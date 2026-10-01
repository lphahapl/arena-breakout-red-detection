"""
路线二：只监控安全箱（安全容器）那一小块区域。

为什么比检测容器搜索面板好：
  * ROI 只有几百像素、位置固定，不需要判断「现在是什么 UI」
  * 没有场景纹理误报 —— 枪皮花纹、天花板格栅、地砖都不会长成
    #331410 的纯色实心块
  * 红品检测器原封不动复用（同一个 #331410 色值）

**关键：安全箱会累积。**
第一个摸到的红放进去后就一直在那儿，所以不能判断「箱里有没有红」——
那会让之后每一次开箱都被误判成出红。必须判断「**新增**了红」：

    基线 N = 首次观察到的红品数
    每次观察：当前数 > N  ->  报告新增，N = 当前数
              当前数 < N  ->  箱子被清空/换局，N 归零重新基线
              其余        ->  静默

跨局时安全箱里的东西是否保留决定了基线怎么重置，见 README。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np

from capture import Grabber
from dataclasses import replace

from detect import Config, red_mask, _core_match_frac

# 整备界面安全箱用的亮度下限。见 count_red 里的说明。
SAFEBOX_VAL_MIN = 40

ROOT = Path(__file__).resolve().parent
BOXCFG = ROOT / "box_config.json"


# --------------------------------------------------------------------------
# 配置存取
# ROI 存成「客户区的比例」，这样换分辨率 / 换窗口大小都不用重标
# --------------------------------------------------------------------------
def load_config() -> dict:
    if BOXCFG.exists():
        return json.loads(BOXCFG.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict) -> None:
    BOXCFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def roi_px(frame_shape, norm_roi: list[float]) -> tuple[int, int, int, int]:
    """把归一化 ROI（比例）换算成当前帧的像素矩形。"""
    h, w = frame_shape[:2]
    x, y, rw, rh = norm_roi
    return (int(x * w), int(y * h), max(int(rw * w), 1), max(int(rh * h), 1))


# --------------------------------------------------------------------------
# 框选（实现已抽到 roi.py，这里只是转发，别处调用点不用改）
# --------------------------------------------------------------------------
def select_roi(frame: np.ndarray, hint: str = "") -> tuple[int, int, int, int] | None:
    from roi import select
    return select(frame, hint=hint, title="select safe box",
                  skip_hint="ESC = cancel")


# --------------------------------------------------------------------------
# 红品计数
# --------------------------------------------------------------------------
def count_red(roi_bgr: np.ndarray, cfg: Config) -> dict:
    """
    数 ROI 里有几个红品槽位。

    复用第 3 层的判据，但面积阈值改成相对 ROI 的面积占比。
    返回 {count, blobs, mask}
    """
    # 安全箱在**整备界面**里是压暗的 UI，红品底色实测 V≈51；
    # 而对局内的容器面板压在亮场景上，红品底色 V≈120~160。
    # track 那条线（对局容器面板）需要高亮度下限来挡住"面板透出的场景底色"，
    # 但那条下限会把安全箱的红品一起挡掉 —— 所以这里单独放宽。
    # 安全箱那条线单独用回小核：close=25 会把**相邻的红格粘成一个**，
    # 导致数少了（实测累积逻辑的用例直接挂）。那条线的格子小、排列紧，
    # 不需要为"被图案劈开"做大合并 —— 那是容器面板才有的问题。
    cfg = replace(cfg, red_val_min=SAFEBOX_VAL_MIN, red_close_win=9)
    mask = red_mask(roi_bgr, cfg)
    h, w = roi_bgr.shape[:2]
    roi_area = float(h * w)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    blobs = []
    for i in range(1, n):
        bx, by, bw, bh = (int(stats[i, 0]), int(stats[i, 1]),
                          int(stats[i, 2]), int(stats[i, 3]))
        area = int(stats[i, 4])
        sel = labels[by:by + bh, bx:bx + bw] == i

        extent = area / float(bw * bh) if bw and bh else 0.0
        aspect = bw / float(bh) if bh else 0.0
        core = _core_match_frac(roi_bgr[by:by + bh, bx:bx + bw], sel, cfg)

        ok = (core >= cfg.red_core_match_min
              and extent >= cfg.red_extent_min
              and cfg.red_aspect[0] < aspect < cfg.red_aspect[1]
              and area / roi_area >= cfg.red_area_frac_min)
        blobs.append({"bbox": (bx, by, bw, bh), "area": area,
                      "core_match": round(core, 3), "extent": round(extent, 3),
                      "accepted": bool(ok)})

    blobs.sort(key=lambda b: (not b["accepted"], -b["area"]))
    return {"count": sum(1 for b in blobs if b["accepted"]),
            "blobs": blobs, "mask": mask, "roi_area": int(roi_area)}


def annotate(roi_bgr: np.ndarray, res: dict) -> np.ndarray:
    vis = roi_bgr.copy()
    for b in res["blobs"]:
        x, y, w, h = b["bbox"]
        color = (0, 255, 255) if b["accepted"] else (90, 90, 90)
        cv2.rectangle(vis, (x, y), (x + w, y + h), color, 2)
        if b["accepted"]:
            cv2.putText(vis, f"core={b['core_match']}", (x, max(y - 5, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    cv2.putText(vis, f"red={res['count']}", (6, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    return vis


# --------------------------------------------------------------------------
# 持续监控
# --------------------------------------------------------------------------
def monitor(grabber: Grabber, norm_roi: list[float], cfg: Config,
            interval: float = 0.6, on_event=None, on_frame=None,
            max_seconds: float | None = None, max_iters: int | None = None):
    """
    持续观察安全箱，检测「新增红品」。

    on_event(kind, count, delta, roi_bgr, res) 在基线建立 / 新增 / 减少时被调用。
        kind: "baseline" | "new" | "decrease"
    on_frame(frame, res, kind, delta) 每轮都调用，用来做旁路记录。

    max_seconds / max_iters 给测试或有界运行封顶，默认都是 None（无限跑）。
    """
    baseline: int | None = None
    t0 = time.perf_counter()
    history: list[dict] = []
    iters = 0

    while True:
        iters += 1
        frame = grabber.grab()
        x, y, w, h = roi_px(frame.shape, norm_roi)
        roi = frame[y:y + h, x:x + w]
        res = count_red(roi, cfg)
        c = res["count"]

        kind, delta = None, 0
        if baseline is None:
            baseline = c
            kind = "baseline"
        elif c > baseline:
            delta = c - baseline
            baseline = c
            kind = "new"
        elif c < baseline:
            delta = baseline - c
            baseline = c
            kind = "decrease"

        if kind and on_event:
            on_event(kind, c, delta, roi, res)
        if on_frame:
            on_frame(frame, res, kind, delta)

        history.append({"t": round(time.perf_counter() - t0, 2), "count": c})

        if max_iters is not None and iters >= max_iters:
            break
        if max_seconds is not None and time.perf_counter() - t0 >= max_seconds:
            break
        time.sleep(interval)

    return {"baseline": baseline, "history": history}
