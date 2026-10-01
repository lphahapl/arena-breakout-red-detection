"""
Web UI 后台运行器的自测：事件记录 + 截图存档 + 采集循环。

不启动真抓屏线程（那需要游戏/视频在前台），用打桩的 Grabber 喂真实截图。

跑法:  python test_runner.py
"""

from __future__ import annotations

import json
import shutil
import time

import cv2
import numpy as np

import runner as runner_mod
from detect import Config, find_red_blobs
from imgio import imread_u          # cv2.imread 在中文路径上读不到，必须用它
from runner import RUNS, Runner
from track import Tracker

SAMPLE = "tests/fixtures/real/sample3_frame.png"   # 真实截图：有机密保险箱 + 一个红品


def _apply_crop(frame, crop):
    """
    模拟真实 Grabber 的 crop 行为：只返回工作区那一块。

    真实链路里 Grabber 抓到的是工作区，Tracker 按「帧即工作区」处理
    （Config.frame_is_work）。打桩的 Grabber 必须做同样的裁剪，
    否则两边对不上 —— 实测踩过：截图存下来变成一张空帧。
    """
    if not crop:
        return frame
    h, w = frame.shape[:2]
    fx, fy, fw, fh = crop
    x0, y0 = int(w * fx), int(h * fy)
    return frame[y0:y0 + int(h * fh), x0:x0 + int(w * fw)].copy()


def _stub(frames, crop):
    """造一个按序列返回帧的打桩 Grabber 类。"""
    class StubGrabber:
        SEQ = frames

        def __init__(self, *a, **kw):
            self.n = 0
            self.window = None
            self.target_name = "stub"
            self.crop = crop

        def grab(self):
            f = self.SEQ[min(self.n, len(self.SEQ) - 1)]
            self.n += 1
            return _apply_crop(f, self.crop)

        def close(self):
            pass

    return StubGrabber


def _crop_of(cfg: Config) -> tuple | None:
    """和 Runner._crop_rect 同一套规则，测试里要用它来构造打桩帧。"""
    if cfg.work_region:
        return tuple(cfg.work_region)
    rf = max(min(cfg.right_frac, 1.0), 0.0)
    if rf >= 1.0:
        return None
    return (round(1.0 - rf, 4), 0.0, round(rf, 4), 1.0)


def _run_with(frames, interval=0.05, miss_need=3, seconds=6, crop=None):
    """用打桩 Grabber 跑一段 Runner，返回 (runner, events)。"""
    orig = runner_mod.Grabber
    runner_mod.Grabber = _stub(frames, crop)
    try:
        cfg = Config()
        cfg.container_keyword = "保险箱"
        r = Runner(cfg, window="", monitor_only=True,
                   interval=interval, miss_need=miss_need)
        r.start()
        time.sleep(seconds)
        r.stop()
        return r, r.events()
    finally:
        runner_mod.Grabber = orig


# --------------------------------------------------------------------------
def test_record() -> bool:
    """事件记录 + 截图存档 + JSONL 落盘（直接调 _record，不起线程）。"""
    run_dir = RUNS / "_selftest"
    if run_dir.exists():
        shutil.rmtree(run_dir)

    r = Runner(Config())
    r.run_dir = run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    ok = True

    class T:
        streak = 0
        total = 0

    def fake_panel(with_red):
        p = np.full((240, 240, 3), 40, np.uint8)
        for i in range(2):
            for j in range(2):
                cv2.rectangle(p, (10 + i * 115, 10 + j * 115),
                              (110 + i * 115, 110 + j * 115), (52, 52, 52), -1)
        if with_red:
            cv2.rectangle(p, (60, 140), (160, 240), (16, 20, 51), -1)   # #331410
        return p

    def mk(event, red, streak, panel):
        blobs = ([{"bbox": (60, 140, 100, 100), "area": 9000, "core_match": 0.83,
                   "extent": 0.9, "accepted": True, "ocr_name": "混沌质",
                   "ocr_text": "混沌质"}] if red else [])
        return ({
            "event": event, "state": "IDLE" if event != "session_start" else "SESSION",
            # name_bbox 是真实截图里量到的位置，_crop_around 靠它算裁剪范围
            "session": {"ocr_text": "电子保险箱", "name_bbox": (37, 161, 105, 21),
                        "red": red, "red_blobs": blobs, "observations": 5},
            "streak": streak, "grid": None, "panel": panel,
        }, blobs)

    for ev, red, st in (("session_start", False, 0),
                        ("container_clean", False, 3),
                        ("container_red", True, 0),
                        (None, False, 0)):          # 中途事件应被忽略
        res, blobs = mk(ev, red, st, fake_panel(red))
        r._record(res, T(), res["panel"], blobs)

    evs = r.events()
    t1 = len(evs) == 3 and [e["kind"] for e in evs] == ["start", "clean", "red"]
    print(f"[{'PASS' if t1 else 'FAIL'}] 只记录 start/clean/red 三类，中途事件被忽略"
          f" -> {[e['kind'] for e in evs]}")
    ok &= t1

    t2 = all(e.get("t") for e in evs)
    print(f"[{'PASS' if t2 else 'FAIL'}] 每条都带时间戳")
    ok &= t2

    files = sorted(p.name for p in run_dir.glob("*.png"))
    t3 = len(files) == 4 and any("red_annotated" in f for f in files)
    print(f"[{'PASS' if t3 else 'FAIL'}] 截图存档 {len(files)} 张（出红多一张标注图）")
    ok &= t3

    an = next(run_dir / f for f in files if "red_annotated" in f)
    raw = next(run_dir / f for f in files
               if f.startswith("0003") and f.endswith("_panel.png"))
    diff = int(np.abs(imread_u(an).astype(int) - imread_u(raw).astype(int)).sum())
    t4 = diff > 0
    print(f"[{'PASS' if t4 else 'FAIL'}] 标注图与原图不同（画了框），像素差 {diff}")
    ok &= t4

    lines = [json.loads(x) for x in
             (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
             if x.strip()]
    t5 = (len(lines) == 3 and lines[2]["red_count"] == 1
          and lines[2]["name_bbox"] == [37, 161, 105, 21]
          and lines[2]["item_names"] == ["混沌质"])
    print(f"[{'PASS' if t5 else 'FAIL'}] events.jsonl 内容正确 "
          f"(red_count / name_bbox / item_names)")
    ok &= t5

    shutil.rmtree(run_dir, ignore_errors=True)
    return ok


def test_loop_integration() -> bool:
    """
    真的把 Runner 的采集线程跑起来（打桩 Grabber + 真实截图）。

    补漏用：之前只测了 _record，_loop 完全没覆盖，结果 _loop 里
    `res["session"]["cells"]` 引用了一个已删字段 —— 第一个容器被识别出来的
    瞬间整个采集线程就崩了，而测试全绿。
    """
    real = imread_u(SAMPLE)
    if real is None:
        print(f"\n[SKIP] 找不到 {SAMPLE}，跳过 _loop 集成测试")
        return True
    blank = np.full_like(real, 20)

    r, evs = _run_with([real] * 5 + [blank] * 8, interval=0.02,
                       miss_need=2, seconds=3, crop=_crop_of(Config()))
    st = r.status()
    kinds = [e["kind"] for e in evs]

    ok = True
    t1 = st["error"] is None
    print(f"\n[{'PASS' if t1 else 'FAIL'}] 采集线程无异常 -> error={st['error']!r}")
    ok &= t1

    t2 = "start" in kinds
    print(f"[{'PASS' if t2 else 'FAIL'}] 识别到容器并开出会话 -> {kinds}")
    ok &= t2

    t3 = "red" in kinds and st["total"] >= 1
    print(f"[{'PASS' if t3 else 'FAIL'}] 走完一次结算，检到红品 -> "
          f"total={st['total']} streak={st['streak']}")
    ok &= t3

    t4 = st["ocr_ms"] > 0
    print(f"[{'PASS' if t4 else 'FAIL'}] 状态里的 OCR 耗时已更新 -> {st['ocr_ms']}ms")
    ok &= t4

    red_ev = next((e for e in evs if e["kind"] == "red"), None)
    files = sorted(f.name for f in r.run_dir.glob("*.png")) if r.run_dir else []
    t5 = bool(red_ev) and any("red_annotated" in f for f in files)
    print(f"[{'PASS' if t5 else 'FAIL'}] 出红事件带标注截图 -> {files}")
    ok &= t5

    # item_names 这里只断言"字段存在且是列表" —— 合成图上的红格没有物品名文字，
    # OCR 读不出东西。名字内容本身在 test_record 里用注入的假名字验证过了。
    t6 = (bool(red_ev) and red_ev["red_count"] >= 1
          and isinstance(red_ev.get("item_names"), list))
    print(f"[{'PASS' if t6 else 'FAIL'}] 出红事件带红品数和 item_names 字段 -> "
          f"red_count={red_ev['red_count'] if red_ev else None} "
          f"item_names={red_ev['item_names'] if red_ev else None}")
    ok &= t6

    big = [f for f in files if (r.run_dir / f).stat().st_size > 400_000]
    t7 = not big
    print(f"[{'PASS' if t7 else 'FAIL'}] 截图已裁剪（都在 400KB 内）")
    ok &= t7

    shutil.rmtree(r.run_dir, ignore_errors=True)
    return ok


def test_two_containers() -> bool:
    """
    连续开两个容器必须产生**两条**记录 —— 不能被并成一条。

    照着一个真实故障写的：用户实测「打开保险箱不止一次，但只记了一条，
    而且缩略图是黑的」。原因是 session_start 返回的 panel 是 None（没截图可存）、
    会话存活判定误读导致结束不了、开箱那一步不扫红品。
    """
    real = imread_u(SAMPLE)
    if real is None:
        print(f"\n[SKIP] 找不到 {SAMPLE}，跳过「连续两个容器」测试")
        return True
    blank = np.full_like(real, 20)

    frames = ([real] * 60 + [blank] * 60) * 2
    r, evs = _run_with(frames, interval=0.05, miss_need=3,
                       seconds=18, crop=_crop_of(Config()))
    st = r.status()
    starts = [e for e in evs if e["kind"] == "start"]

    ok = True
    t1 = st["total"] == 2
    print(f"\n[{'PASS' if t1 else 'FAIL'}] 两个容器都结算了 -> total={st['total']} "
          f"(期望 2)  事件={[e['kind'] for e in evs]}")
    ok &= t1

    t2 = len(starts) == 2
    print(f"[{'PASS' if t2 else 'FAIL'}] 两条开箱记录（没被并成一条）-> {len(starts)} 条")
    ok &= t2

    t3 = all(e["shots"] for e in starts)
    print(f"[{'PASS' if t3 else 'FAIL'}] 每条开箱都有截图（黑缩略图 bug）-> "
          f"{[e['shots'] for e in starts]}")
    ok &= t3

    t4 = all(e["red_count"] >= 1 for e in evs if e["kind"] == "red")
    print(f"[{'PASS' if t4 else 'FAIL'}] 两次都检到红品 -> "
          f"{[(e['kind'], e['red_count']) for e in evs if e['kind'] == 'red']}")
    ok &= t4

    shutil.rmtree(r.run_dir, ignore_errors=True)
    return ok


def _probe_hit(real):
    """在真实样本上定位：面板矩形、工作区、红品格子。"""
    cfg = Config()
    cfg.container_keyword = "保险箱"
    cfg.ocr_interval = 0
    tr = Tracker(cfg)
    tr.step(real)
    if not tr._anchor:
        return None
    return tr, tr.panel_rect(), tr.work_rect(real)[:2], tr._work(real)


def test_red_screenshot_timing() -> bool:
    """
    出红事件的截图必须来自**红品出现的那一刻**，不是会话结束时那一帧。

    真实故障：出红的标注图是一张无关的游戏场景（角色肩膀+木墙），
    黄框框着空白。原因是截图在会话结算时才取，那时画面早飘走了。
    """
    real = imread_u(SAMPLE)
    if real is None:
        print(f"\n[SKIP] 找不到 {SAMPLE}，跳过「出红截图时机」测试")
        return True

    got = _probe_hit(real)
    if got is None:
        print("\n[FAIL] 样本里没有红品，测试构造不出来")
        return False
    _, pr, (wx, wy), wk = got
    x, y, w, h = pr
    cfg = Config()
    cfg.container_keyword = "保险箱"
    hit = next((b for b in find_red_blobs(wk[y:y + h, x:x + w], cfg)["blobs"]
                if b["accepted"]), None)
    if hit is None:
        print("\n[FAIL] 样本里没有红品，测试构造不出来")
        return False

    # 开场帧：把红品格子用面板底色盖掉（-> 检不到红），再画个**纯白方块**当标记。
    # 用白块而不是染色 —— 染色会连红品一起染，开场帧也会触发红检，分不清快照
    # 到底存的是哪一帧（第一版就栽在这上面，B=16 G=17 几乎全黑却判"通过"）。
    bx, by, bw, bh = hit["bbox"]
    plain = wk.copy()
    plain[y + by:y + by + bh, x + bx:x + bx + bw] = (40, 40, 40)
    cv2.rectangle(plain, (x + w - 260, y + h - 260), (x + w - 60, y + h - 60),
                  (255, 255, 255), -1)
    opening = real.copy()
    opening[wy:wy + wk.shape[0], wx:wx + wk.shape[1]] = plain
    redframe = real.copy()
    gone = np.full_like(real, 20)

    frames = [opening] * 8 + [redframe] * 8 + [gone] * 8
    r, evs = _run_with(frames, interval=0.03, miss_need=3,
                       seconds=6, crop=_crop_of(Config()))

    red_ev = next((e for e in evs if e["kind"] == "red"), None)
    ok = True
    t1 = bool(red_ev) and bool(red_ev["shots"])
    print(f"\n[{'PASS' if t1 else 'FAIL'}] 出红事件有截图 -> "
          f"{red_ev['shots'] if red_ev else None}")
    ok &= t1
    if not t1:
        shutil.rmtree(r.run_dir, ignore_errors=True)
        return False

    shot = imread_u(r.run_dir / red_ev["shots"][0])

    # 不能用一个绝对阈值 —— 样本画面本身就有约 2% 的白色像素（白字、高光、字幕）。
    # 改成和两帧各自的基线比：截图应当更接近**红品帧**而不是开场帧。
    def crop_of(frame):
        return frame[wy:wy + wk.shape[0], wx:wx + wk.shape[1]][y:y + h, x:x + w]

    def white_frac(img):
        return float(((img[:, :, 0] > 200) & (img[:, :, 1] > 200)
                      & (img[:, :, 2] > 200)).mean())

    base_red, base_open = white_frac(crop_of(redframe)), white_frac(crop_of(opening))
    wf = white_frac(shot)
    t2 = abs(wf - base_red) < abs(wf - base_open)
    print(f"[{'PASS' if t2 else 'FAIL'}] 截图更接近红品帧而不是开场帧 -> "
          f"白像素 {wf:.2%}  基线(红品帧) {base_red:.2%}  基线(开场帧) {base_open:.2%}")
    ok &= t2

    t3 = float(shot.mean()) > 40
    print(f"[{'PASS' if t3 else 'FAIL'}] 截图不是空帧 -> 均值亮度 {shot.mean():.0f}")
    ok &= t3

    shutil.rmtree(r.run_dir, ignore_errors=True)
    return ok


def test_clean_screenshot_timing() -> bool:
    """
    无红事件的截图必须是**面板还在**的那一帧。

    真实故障：无红的截图是一张木梁天花板。原因是会话结束前那几轮模板匹配
    已经失败（面板早没了），但 SESSION 状态照样返回工作区图，
    last_panel 被那些帧覆盖了。
    """
    real = imread_u(SAMPLE)
    if real is None:
        print(f"\n[SKIP] 找不到 {SAMPLE}，跳过「无红截图时机」测试")
        return True

    got = _probe_hit(real)
    if got is None:
        print("\n[FAIL] 样本里没有红品，测试构造不出来")
        return False
    _, pr, (wx, wy), wk = got
    x, y, w, h = pr
    cfg = Config()
    cfg.container_keyword = "保险箱"
    hit = next((b for b in find_red_blobs(wk[y:y + h, x:x + w], cfg)["blobs"]
                if b["accepted"]), None)
    if hit is None:
        print("\n[FAIL] 样本里没有红品，测试构造不出来")
        return False

    # 面板帧：盖掉红品（-> 无红），再画个白块确保这帧有可辨认特征
    bx, by, bw, bh = hit["bbox"]
    plain = wk.copy()
    plain[y + by:y + by + bh, x + bx:x + bx + bw] = (40, 40, 40)
    cv2.rectangle(plain, (x + w - 260, y + h - 260), (x + w - 60, y + h - 60),
                  (255, 255, 255), -1)
    panel_frame = real.copy()
    panel_frame[wy:wy + wk.shape[0], wx:wx + wk.shape[1]] = plain
    gone = np.full_like(real, 20)

    frames = [panel_frame] * 8 + [gone] * 12
    r, evs = _run_with(frames, interval=0.03, miss_need=3,
                       seconds=6, crop=_crop_of(Config()))

    clean_ev = next((e for e in evs if e["kind"] == "clean"), None)
    ok = True
    t1 = bool(clean_ev) and bool(clean_ev["shots"])
    print(f"\n[{'PASS' if t1 else 'FAIL'}] 无红事件有截图 -> "
          f"{clean_ev['shots'] if clean_ev else None}")
    ok &= t1
    if not t1:
        shutil.rmtree(r.run_dir, ignore_errors=True)
        return False

    shot = imread_u(r.run_dir / clean_ev["shots"][0])
    t2 = float(shot.mean()) > 40
    print(f"[{'PASS' if t2 else 'FAIL'}] 截图不是「面板已消失」的帧 -> "
          f"均值亮度 {shot.mean():.0f}（空帧是 20）")
    ok &= t2

    wf = float(((shot[:, :, 0] > 200) & (shot[:, :, 1] > 200)
                & (shot[:, :, 2] > 200)).mean())
    t3 = wf > 0.05
    print(f"[{'PASS' if t3 else 'FAIL'}] 截图里有面板帧的白块标记 -> "
          f"白像素占比 {wf:.2%}（期望 >5%）")
    ok &= t3

    shutil.rmtree(r.run_dir, ignore_errors=True)
    return ok


def main() -> int:
    print("=== 事件记录 + 截图存档 ===")
    ok = test_record()
    ok &= test_loop_integration()
    ok &= test_two_containers()
    ok &= test_red_screenshot_timing()
    ok &= test_clean_screenshot_timing()
    print("\n全部通过" if ok else "\n有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
