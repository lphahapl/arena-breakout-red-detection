"""
原型 demo：检测暗区突围无限的容器 UI，再在面板里找红色物品色块。

用法:
    python main.py list                    列出可见窗口，找游戏窗口标题
    python main.py snap                    抓一帧分析，把全部中间图存到 debug/
    python main.py watch                   持续运行，状态变化时打印
    python main.py snap --window 暗区      按标题子串指定游戏窗口
    python main.py snap --monitor          不找窗口，直接抓主显示器

snap 会保存到 debug/<时间戳>/ ：
    00_full.png        完整帧
    01_right.png       右侧工作区（左边背包已被裁掉）
    02_edges.png       Canny 边缘
    03_density.png     边缘密度图
    04_hot.png         密度阈值化 + 闭运算的结果（候选面板）
    05_grid.png        面板检测结果叠加（绿=通过，红=未通过）
    06_col_profile.png 竖直边缘投影曲线（看周期性）
    07_row_profile.png 水平边缘投影曲线
    08_redmask.png     红色掩码
    09_candidates.png  色块判定叠加（黄框=命中，灰框=被拒+原因）
    10_panel.png       面板原图裁剪
    cand_XX_*.png      每个候选色块的近距裁剪（原图 + 掩码）
    report.json        全部实测数值
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import safebox
from capture import (Grabber, window_visible_ratio, list_windows,
                     load_settings, resolve_window)
from detect import Config, analyze, config_to_dict
from imgio import imwrite_u

ROOT = Path(__file__).resolve().parent
DEBUG_ROOT = ROOT / "debug"


# --------------------------------------------------------------------------
def dump_debug(res: dict, tag: str = "") -> Path:
    """把一次分析的所有中间结果落盘。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{tag}" if tag else "")
    out = DEBUG_ROOT / stamp
    out.mkdir(parents=True, exist_ok=True)

    for name, img in res["debug"].items():
        if img is None:
            continue
        imwrite_u(out / f"{name}.png", img)

    # 每个候选色块单独裁剪，原始 + 掩码并排
    panel = res["debug"].get("10_panel")
    mask = res["debug"].get("08_redmask")
    for i, b in enumerate(res.get("blobs", [])):
        if panel is None or mask is None:
            break
        bx, by, bw, bh = b["bbox"]
        pad = 6
        x1, y1 = max(bx - pad, 0), max(by - pad, 0)
        x2, y2 = min(bx + bw + pad, panel.shape[1]), min(by + bh + pad, panel.shape[0])
        crop = panel[y1:y2, x1:x2]
        mcrop = cv2.cvtColor(mask[y1:y2, x1:x2], cv2.COLOR_GRAY2BGR)
        if crop.size:
            side = np.hstack([crop, mcrop])
            imwrite_u(out / f"cand_{i:02d}_{'HIT' if b['accepted'] else 'rej'}.png", side)

    report = {
        "tag": tag,
        "time": stamp,
        "frame_size": res["frame_size"],
        "right_abs": res["right_abs"],
        "right_frac": res["right_frac"],
        "ui_found": res["ui_found"],
        "red_found": res["red_found"],
        "grid": res["grid"],
        "panel_abs": res.get("panel_abs"),
        "accepted_abs": res["accepted"],
        "blobs": res.get("blobs", []),
        "config": config_to_dict(CFG),
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# 依赖自检
#
# 为什么这段中文不写在 .bat 里：cmd.exe 按**控制台代码页**（简中系统是 GBK）
# 解析 .bat 文件，而文件是 UTF-8 存的，中文会变成乱码 —— 而且乱码会把命令行
# 本身打散（实测 `python` 被解析成 `'ython'`、`echo` 变成 `'cho'`，还会去执行
# 提示语里提到的文件名）。所以 bat 里只留纯 ASCII，中文一律由 Python 打印，
# Python 走 Windows 控制台 API 输出，没有编码问题。
# --------------------------------------------------------------------------
def cmd_deps(_args) -> int:
    print()
    print("=" * 62)
    print("  依赖自检")
    print("=" * 62)
    print()

    ok = True

    try:
        import mss
        import numpy
        import cv2
        print(f"  抓屏 / 图像库        OK")
        print(f"      mss {mss.__version__}   numpy {numpy.__version__}   "
              f"opencv {cv2.__version__}")
    except ImportError as e:
        ok = False
        print(f"  抓屏 / 图像库        缺失 -> {e}")
        print(f"      跑:  python -m pip install mss numpy opencv-python")

    print()
    try:
        import ocr
        langs = ocr.available_languages()
        has_cn = "zh-Hans-CN" in langs
        print(f"  Windows OCR          {'OK' if has_cn else '缺中文语言包'}")
        print(f"      可用语言: {langs}")
        if not has_cn:
            ok = False
            print("      去【设置 > 时间和语言 > 语言】添加「中文(简体)」")
    except SystemExit as e:
        ok = False
        print(f"  容器名字 OCR         不可用")
        print(f"      {e}")
        print("      跑:  python -m pip install winrt-runtime "
              "winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging "
              "winrt-Windows.Storage winrt-Windows.Storage.Streams "
              "winrt-Windows.Globalization winrt-Windows.Foundation "
              "winrt-Windows.Foundation.Collections")

    print()
    print("=" * 62)
    if ok:
        print("  自检通过。")
        print()
        print("  下一步：跑『2-诊断容器名字.bat』—— 打开一个保险箱搜索面板，")
        print("          它会识别面板上的容器名字并写出日志。")
        print("          把 logs 文件夹发我。")
    else:
        print("  有项目没通过，先解决上面的问题。")
    print("=" * 62)
    print()
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 清掉残留的服务进程
# --------------------------------------------------------------------------
def cmd_stop_all(_args) -> int:
    """
    杀掉所有正在监听默认端口的本工具 serve 进程。

    为什么需要：Python 的 HTTPServer 设了 allow_reuse_address，多个进程能绑
    同一端口。实测用户双击几次 bat 就攒了三个 serve，其中一个是旧版本 ——
    表现为"代码改好了却还是旧行为"，而且刷新几次结果还不一样。
    现在启动时会检测并复用，但已经留下的旧实例得有个办法清掉。
    """
    import subprocess

    from webui import DEFAULT_PORT

    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"跑 netstat 失败: {e}")
        return 1

    pids = set()
    for line in out.splitlines():
        if f":{DEFAULT_PORT}" in line and "LISTENING" in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])

    if not pids:
        print(f"端口 {DEFAULT_PORT} 上没有残留的服务进程。")
        return 0

    print(f"发现 {len(pids)} 个残留在 {DEFAULT_PORT} 上的服务进程: {sorted(pids)}")
    killed = 0
    for pid in sorted(pids):
        r = subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, text=True)
        if r.returncode == 0:
            killed += 1
            print(f"  已终止 PID {pid}")
        else:
            print(f"  终止 PID {pid} 失败: {(r.stderr or r.stdout).strip()[:60]}")
    print(f"\n清掉 {killed} 个。现在可以重新启动 3-开始检测.bat 了。")
    return 0


# --------------------------------------------------------------------------
# 工作区框选
# --------------------------------------------------------------------------
def cmd_select_work(args) -> int:
    """单独框选工作区并存起来（不想每次启动被问就手动跑一次）。"""
    g = _make_grabber(args)
    try:
        print(f"抓取目标: {g.target_name}")
        if g.using_fallback:
            print("!! 没找到游戏窗口，退回抓显示器。")
        r = pick_work_region(g)
        if r:
            print(f"\n完成。以后启动会自动用 {r}")
            print("想重框: 再跑一次这个命令")
            print("想回到默认: 加 --no-region")
        else:
            print("\n已跳过，保持默认（屏幕右侧 40%）。")
        return 0
    finally:
        g.close()


def _work_rect(frame) -> tuple[int, int, int, int]:
    """工作区在整帧里的像素矩形。和 Tracker.work_rect 同一套规则。"""
    h, w = frame.shape[:2]
    if CFG.work_region:
        from roi import to_pixels
        return to_pixels(tuple(CFG.work_region), frame.shape)
    x0 = int(round(w * (1.0 - CFG.right_frac)))
    return (x0, 0, max(w - x0, 1), h)


def _countdown(seconds: int) -> None:
    for i in range(seconds, 0, -1):
        print(f"  {i}...", end="", flush=True)
        time.sleep(1)
    print(" 抓取!")


def pick_work_region(grabber, save: bool = True, seconds: int = 8):
    """
    倒计时 -> 抓帧 -> 框选 -> 存进 settings.json。

    返回归一化比例 (fx,fy,fw,fh)；用户按 Esc 跳过则返回 None（= 用默认）。
    """
    import roi

    print()
    print("【框选工作区】")
    print("  框出「容器面板会出现的那块区域」—— 搜刮时显示物品格子的地方。")
    print("  圈得准有两个好处：区域小 -> OCR 和网格检测都更快；")
    print("                    范围窄 -> 背包/场景的干扰进不来，误报更低。")
    print("  不想框就直接在弹出窗口里按 Esc，会用默认值（屏幕右侧 40%）。")
    print()
    input("  >>> 按回车开始倒计时（之后切回游戏，打开一个保险箱面板）...")
    _countdown(seconds)

    frame = grabber.grab()
    rect = roi.select(
        frame,
        hint="DRAG to box the CONTAINER PANEL area",
        title="work region",
        skip_hint="ESC = use default (right 40%)",
    )
    if rect is None:
        print("  已跳过，用默认工作区（右侧 40%）。")
        return None

    norm = roi.to_normalized(rect, frame.shape)
    print(f"  已框选: {rect}  归一化 {norm}")
    if save:
        s = load_settings()
        s["work_region"] = list(norm)
        s["work_region_frame"] = [frame.shape[1], frame.shape[0]]
        from capture import save_settings
        save_settings(s)
        print("  已存进 settings.json，之后启动会直接用。")
    return norm


def maybe_pick_region(args) -> None:
    """
    启动时的流程：
      有保存过的工作区 -> 直接用，顺便问要不要重框
      没保存过         -> 问要不要现在框
    一律可以跳过，跳过就用默认（屏幕右侧 40%）。
    """
    if args.monitor:
        return

    s = load_settings()
    saved = s.get("work_region")
    if saved and not args.no_region:
        CFG.work_region = tuple(saved)
        print(f"\n工作区: 已保存的 {tuple(saved)}"
              f"（框于 {s.get('work_region_frame')}）")
    else:
        print(f"\n工作区: 默认右侧 {CFG.right_frac:.0%}"
              f"（左边是背包，不看）{'  [--no-region 已忽略保存值]' if saved else ''}")

    if args.no_pick:
        return

    if saved and not args.no_region:
        try:
            ans = input("  重新框选? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans != "y":
            return
    else:
        try:
            ans = input("  现在手动框选工作区? 框准了更快更准 [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans == "n":
            return

    g = _make_grabber(args)
    try:
        r = pick_work_region(g)
        if r:
            CFG.work_region = r
    finally:
        g.close()


# --------------------------------------------------------------------------
def _rect_close(a, b, rel_tol: float = 0.05, abs_tol: int = 6) -> bool:
    """两个面板矩形是否算「同一位置」。容差随面板尺寸缩放。"""
    for i in range(4):
        tol = max(abs_tol, rel_tol * max(a[2], a[3], 1))
        if abs(a[i] - b[i]) > tol:
            return False
    return True


# --------------------------------------------------------------------------
def summarize(res: dict) -> str:
    if not res["ui_found"]:
        g = res["grid"]
        return f"未检测到容器面板 | {g['reason'] if g else '?'}"
    n_hit = len(res["accepted"])
    n_all = len(res.get("blobs", []))
    px, py = res["grid"]["period"]
    return (f"面板 {res['panel_abs'][2]}x{res['panel_abs'][3]} "
            f"格子 {px}x{py}px | 候选 {n_all} 个, 命中 {n_hit} 个"
            + ("" if n_hit == 0 else f"  abs={res['accepted']}"))


# --------------------------------------------------------------------------
def cmd_list(_args) -> int:
    ws = list_windows(min_side=400)
    if not ws:
        print("没找到合适的窗口")
        return 1
    print(f"{'hwnd':>10}  {'client':>12}  title")
    print("-" * 70)
    for w in sorted(ws, key=lambda x: -x["w"] * x["h"]):
        print(f"{w['hwnd']:>10}  {w['w']:>5}x{w['h']:<6}  {w['title']}")
    print("\n用  --window <标题子串>  指定，或直接跑 --window 用前台窗口")
    return 0


def _make_grabber(args) -> Grabber:
    # 从 bat 双击运行时，前台窗口是控制台自己 —— 直接抓会抓到黑窗口。
    # 所以没指定 --window 时先解析：命令行 > settings.json > 列窗口让用户输编号。
    if not args.monitor and not args.window:
        args.window = resolve_window()
    return Grabber(window_substr=args.window,
                   monitor=args.monitor_index,
                   force_monitor=args.monitor)


def cmd_snap(args) -> int:
    g = _make_grabber(args)
    try:
        print(f"目标: {g.target_name}")
        if g.using_fallback:
            print("!! 没找到游戏窗口，退回抓显示器。请确保游戏无边框全屏且在最前面，"
                  "否则 0.6/0.4 的比例对不上。")
        frame = g.grab()
        t0 = time.perf_counter()
        res = analyze(frame, CFG)
        dt = (time.perf_counter() - t0) * 1000
        out = dump_debug(res, tag="snap")
        print(f"分析耗时 {dt:.1f} ms")
        print(summarize(res))
        if res["ui_found"]:
            print("注意: snap 是单帧判定，无法排除「场景里的周期性纹理」"
                  "（枪械皮肤花纹 / 天花板格栅 / 地砖）。"
                  "确认请用 watch 模式，它会额外要求面板位置连续多帧稳定。")
        print(f"debug 输出: {out}")
        return 0
    finally:
        g.close()


def cmd_watch(args) -> int:
    g = _make_grabber(args)
    interval = 1.0 / max(args.fps, 0.1)
    print(f"目标: {g.target_name}   轮询 {args.fps} fps   Ctrl+C 退出")
    if g.using_fallback:
        print("!! 没找到游戏窗口，退回抓显示器。")

    state = "IDLE"
    prev = state
    streak = 0
    # 位置稳定性：面板必须在同一个位置连续出现若干帧才算数。
    # 这是区分「真 UI」和「场景里周期性纹理」的关键判据 —— 枪械皮肤花纹、
    # 天花板格栅、地砖都有周期，但你跑动时它们在移动，而搜刮 UI 是钉死的。
    anchor: tuple | None = None      # 上一次的面板矩形
    stable = 0                       # 连续稳定的帧数
    STABLE_NEED = 3                  # 需要连续几帧（@2fps 约 1.5 秒）

    try:
        while True:
            t0 = time.perf_counter()
            frame = g.grab()
            res = analyze(frame, CFG)

            if res["ui_found"]:
                r = res["grid"]["rect_rel_right"]
                if anchor is not None and _rect_close(r, anchor):
                    stable += 1
                else:
                    anchor, stable = r, 1
            else:
                anchor, stable = None, 0

            if state == "IDLE" and stable >= STABLE_NEED:
                state = "PANEL"
            elif state == "PANEL" and stable == 0:
                state = "IDLE"
                streak = 0

            if res["red_found"] and state == "PANEL":
                streak += 1
                if streak == 1:      # 同一个面板只报一次
                    print(f"[{datetime.now():%H:%M:%S}] *** 红品命中 *** {summarize(res)}")
                    dump_debug(res, tag="hit")

            if state != prev:
                print(f"[{datetime.now():%H:%M:%S}] 状态 -> {state} | {summarize(res)}")
                prev = state
                if args.save_all:
                    dump_debug(res, tag=state.lower())
            elif args.verbose:
                mark = "*" if res["ui_found"] else " "
                print(f"[{datetime.now():%H:%M:%S}]{mark} stable={stable} | {summarize(res)}")

            dt = time.perf_counter() - t0
            time.sleep(max(0.0, interval - dt))
    except KeyboardInterrupt:
        print("\n已退出")
        return 0
    finally:
        g.close()


# --------------------------------------------------------------------------
# 容器名字 OCR 诊断
# --------------------------------------------------------------------------
def cmd_name(args) -> int:
    """
    抓帧 -> OCR 右侧区域 -> 打印并**写进日志文件**。

    用途有两个：
      1. 确认游戏里那三个字到底怎么写（你写「保险箱」，我在整备界面上 OCR 到的是「安全箱」）
      2. 拿到名字在屏幕上的 bbox —— 只 OCR 那一小块能把 126ms 降到 ~10ms

    跑完把 logs/ 目录整个发我即可，日志里什么都有。
    """
    import ocr

    g = _make_grabber(args)
    lines_out: list[str] = []

    def say(s: str = "") -> None:
        print(s)
        lines_out.append(s)

    # ocr_region 的留白要按文字高度算，不能给固定小值。
    # 实测：文字高 15px 时，上下留白 <25px 会**直接识别不到**（0 行），
    # 25px 才勉强命中，30px 稳定。横向同理，60px 起。
    # 原因是 Windows OCR 的行检测需要文字周围有足够的空白上下文。
    def pad_for(hw: int, hh: int) -> tuple[int, int]:
        return max(100, hw), max(35, int(hh * 2.5))

    try:
        say("=" * 66)
        say("  容器名字 OCR 诊断")
        say("=" * 66)
        say(f"时间     : {datetime.now():%Y-%m-%d %H:%M:%S}")
        say(f"抓取目标 : {g.target_name}")
        say(f"工作区   : " + (f"手动画的 {tuple(CFG.work_region)}"
                                 if CFG.work_region else
                                 f"默认右侧 {CFG.right_frac:.0%}"))
        say(f"当前关键词: {CFG.container_keyword!r}")
        say()
        say("【你要做的事】")
        say("  1. 先切到游戏，打开一个【保险箱】的搜索面板（就是你要统计的那种容器）")
        say("  2. 让面板停在画面上别关")
        say("  3. 切回这个黑窗口按回车 —— 之后有 8 秒倒计时")
        say("  4. 在这 8 秒内切回游戏，保持面板可见")
        say()
        say("  如果不好切：把游戏设成无边框窗口化，就能和这个窗口并排。")
        say()
        input("  >>> 准备好了就按回车开始倒计时...")

        say()
        for i in range(8, 0, -1):
            print(f"  {i}...", end="", flush=True)
            time.sleep(1)
        print(" 抓取!")

        frame = g.grab()
        H, W = frame.shape[:2]
        wx, wy, rw0, rh0 = _work_rect(frame)
        right = frame[wy:wy + rh0, wx:wx + rw0]

        t0 = time.perf_counter()
        lines = ocr.recognize(right, upscale=CFG.ocr_upscale, lang_tag=CFG.ocr_lang)
        dt = (time.perf_counter() - t0) * 1000

        logdir = ROOT / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot = logdir / f"name_{stamp}_frame.png"
        imwrite_u(shot, frame)
        imwrite_u(logdir / f"name_{stamp}_right.png", right)

        say()
        say("-" * 66)
        say(f"整帧尺寸   : {W}x{H}")
        say(f"工作区区域 : {right.shape[1]}x{right.shape[0]}  "
            f"(整帧坐标 x={wx}, y={wy})")
        say(f"OCR 耗时   : {dt:.0f} ms")
        say(f"识别到行数 : {len(lines)}")
        say("-" * 66)
        say()
        say("识别结果（flat 是去掉空白后的形式，OCR 会在汉字间插空格）：")
        say()

        hit_any = False
        for i, ln in enumerate(lines):
            flat = ocr.flatten(ln["text"])
            bbox = tuple(round(v) for v in ln["bbox"])
            mark = ""
            if CFG.container_keyword and CFG.container_keyword in flat:
                mark = "   <<<< 命中关键词"
                hit_any = True
            say(f"  [{i:02d}] flat = {flat!r}{mark}")
            say(f"       raw   = {ln['text']!r}")
            say(f"       bbox  = {bbox}   (相对工作区左上角, x y w h)")
            say(f"       整帧  = ({bbox[0] + wx}, {bbox[1] + wy}, {bbox[2]}, {bbox[3]})")
            say()

        say("-" * 66)
        if hit_any:
            say(f"结论：有行命中了 {CFG.container_keyword!r}。")
            say("      如果命中的正是保险箱那一行，说明关键词可以直接用了。")
        else:
            say(f"结论：没有任何行命中 {CFG.container_keyword!r}。")
            say("      把上面某个 flat 的值照抄给我，我按真实文本改关键词。")

        # 给出可直接粘贴的 ocr_region —— 只 OCR 这一小块能把 144ms 降到 ~9ms
        # 取**第一个**命中的行：命中多行是常态（「安全箱」和「钛金安全箱」
        # 都会中），不能假设只有一个。
        matched = [ln for ln in lines
                   if CFG.container_keyword in ocr.flatten(ln["text"])]
        pick = (matched or lines)[0] if (matched or lines) else None
        if pick is None:
            say("没有识别到任何文字，给不出 ocr_region 建议。")
            say()
        hx, hy, hw, hh = (round(v) for v in (pick["bbox"] if pick else (0, 0, 0, 0)))
        pad_x, pad_y = pad_for(hw, hh)
        RX, RY = right.shape[1], right.shape[0]
        fx = max(hx - pad_x, 0) / RX
        fy = max(hy - pad_y, 0) / RY
        fw = min((hw + pad_x * 2) / RX, 1.0 - fx)
        fh = min((hh + pad_y * 2) / RY, 1.0 - fy)
        region = (round(fx, 4), round(fy, 4), round(fw, 4), round(fh, 4))
        say()
        say("-" * 66)
        say("提速建议：把 OCR 范围缩到名字那一行，实测 144ms -> 9ms（15.9 倍）")
        say("      下面是归一化比例（相对工作区），留白已按文字高度算好。")
        say("      可直接粘贴使用：")
        say()
        say(f"      python main.py --window 暗区 track "
            f"--ocr-region {region[0]},{region[1]},{region[2]},{region[3]}")
        say()
        say(f"      或者直接设 Config.ocr_region = {region}")
        say()
        say(f"截图已存   : {shot.name}  (整帧)")
        say(f"             name_{stamp}_right.png  (右侧区域)")
        say(f"日志已存   : name_{stamp}.log")
        say()
        say(">>> 把整个 logs 目录发我即可。")

        (logdir / f"name_{stamp}.log").write_text("\n".join(lines_out),
                                                  encoding="utf-8")
        return 0
    finally:
        g.close()


# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------
def cmd_serve(args) -> int:
    import webui
    from runner import Runner

    maybe_pick_region(args)

    win = args.window or ("" if args.monitor else load_settings().get("window", ""))
    r = Runner(CFG, window=win, monitor_only=args.monitor,
               monitor_index=args.monitor_index, interval=args.interval,
               miss_need=args.miss_need)
    if not r.window and not args.monitor:
        print("提示: 没指定 --window，请在网页里选一个游戏窗口再点开始。")
        print("      （否则会退化成抓前台窗口，而前台通常是浏览器自己）")
    webui.serve(r, host=args.host, port=args.port,
                open_browser=not args.no_browser)
    return 0


# --------------------------------------------------------------------------
# 完整链路：保险箱计数 + 出红判定
# --------------------------------------------------------------------------
def cmd_track(args) -> int:
    import track as trackmod

    maybe_pick_region(args)

    g = _make_grabber(args)
    print(f"目标: {g.target_name}")
    print(f"关键词 {CFG.container_keyword!r}   "
          f"结束去抖 {args.miss_need} 轮")
    print("链路: OCR 认容器名字 -> 锚定面板 -> 色块检测找红品")
    print("Ctrl+C 退出\n")

    tr = trackmod.Tracker(CFG, miss_need=args.miss_need)
    last_panel = None
    # 游戏不在前台时喂空帧：抓屏抓的是桌面合成结果，别的窗口盖住游戏时
    # 拿到的就是那个窗口。喂空帧能让状态机走 miss 自然结束掉未完成的会话。
    blank = np.zeros((64, 64, 3), np.uint8)
    warned_fg = False
    try:
        while True:
            t0 = time.perf_counter()
            fg = True
            if g.window is not None:
                fg = window_visible_ratio(g.window["hwnd"], g.region()) >= 0.6
            if not fg and not warned_fg:
                print("  (游戏不在前台，暂停分析；切回游戏即恢复)")
                warned_fg = True
            elif fg:
                warned_fg = False
            frame = g.grab() if fg else blank
            res = tr.step(frame)
            ev = res["event"]
            ts = f"{datetime.now():%H:%M:%S}"

            if res["panel"] is not None:
                last_panel = res["panel"]

            s = res["session"]
            if ev == "session_start":
                nb = s.get("name_bbox")
                print(f"[{ts}] 开箱  {s['ocr_text']}"
                      + (f"   名字@{nb}" if nb else ""))
            elif ev == "container_red":
                print(f"[{ts}] *** 出红 ***  {s['ocr_text']} "
                      f"命中 {len(s.get('red_blobs', []))} 个色块")
                print(f"         连续 {res['streak']} 个保险箱未出红，此处中断")
                out = DEBUG_ROOT / f"track_{datetime.now():%Y%m%d_%H%M%S}_red"
                out.mkdir(parents=True, exist_ok=True)
                if last_panel is not None:
                    imwrite_u(out / "panel.png", last_panel)
                (out / "session.json").write_text(
                    json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"         debug: {out}")
            elif ev == "container_clean":
                print(f"[{ts}] 无红  {s['ocr_text']}"
                      f"   -> 连续 {res['streak']} 个未出红")
            elif args.verbose:
                note = f"  ({res['note']})" if res.get("note") else ""
                print(f"[{ts}] {res['state']}{note}")

            time.sleep(max(0.0, args.interval - (time.perf_counter() - t0)))
    except KeyboardInterrupt:
        print(f"\n{tr.summary()}")
        if tr.history:
            out = DEBUG_ROOT / f"track_{datetime.now():%Y%m%d_%H%M%S}_history"
            out.mkdir(parents=True, exist_ok=True)
            (out / "history.json").write_text(
                json.dumps(tr.history, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"完整记录: {out / 'history.json'}")
    finally:
        g.close()
    return 0


# --------------------------------------------------------------------------
# 路线二：安全箱监控
# --------------------------------------------------------------------------
def cmd_select_box(args) -> int:
    g = _make_grabber(args)
    try:
        print(f"目标: {g.target_name}")
        frame = g.grab()
        print("弹出窗口里拖动鼠标框选【安全箱】区域（只框箱子本身，别把背包框进来）")
        print("回车/空格确认，Esc 取消")
        rect = safebox.select_roi(frame, hint="box the SAFE BOX only, NOT the backpack")
        if rect is None:
            print("已取消")
            return 1
        h, w = frame.shape[:2]
        x, y, rw, rh = rect
        norm = [round(x / w, 4), round(y / h, 4), round(rw / w, 4), round(rh / h, 4)]
        c = safebox.load_config()
        c["roi"] = norm
        c["saved_at"] = datetime.now().isoformat(timespec="seconds")
        c["frame_size"] = [w, h]
        c["target"] = g.target_name
        safebox.save_config(c)
        print(f"已保存 box_config.json")
        print(f"  像素 {rect}  (在 {w}x{h} 下)")
        print(f"  归一化 {norm}  <- 存的是比例，换分辨率不用重标")
        return 0
    finally:
        g.close()


def cmd_box(args) -> int:
    conf = safebox.load_config()
    if "roi" not in conf:
        print("还没标定安全箱位置。先跑一次:")
        print("    python main.py --window 暗区 select-box")
        return 1

    g = _make_grabber(args)
    print(f"目标: {g.target_name}")
    print(f"安全箱 ROI: {conf['roi']}  (标定于 {conf.get('saved_at', '?')})")
    print(f"轮询 {1.0/args.interval:.1f} fps    Ctrl+C 退出")
    print("注意：安全箱会累积，所以这里只报「新增」，不报「有没有」。")

    n_new = 0

    # 旁路记录：每轮跑一次 OCR，把「安全箱红品数 + 容器名字有没有出现 + 名字位置」
    # 逐帧写进 JSONL。跑一局后离线分析，可以核对锚点稳不稳、名字位置会不会变。
    log_path = None
    log_f = None
    if args.log_containers:
        DEBUG_ROOT.mkdir(parents=True, exist_ok=True)
        log_path = DEBUG_ROOT / f"containers_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        log_f = log_path.open("w", encoding="utf-8")
        print(f"容器日志: {log_path}")
        print("  （这会额外跑一次 OCR，全区域约 300ms/次）")

    def on_frame(frame, box_res, kind, delta):
        if log_f is None:
            return
        # 网格检测已从主链路拿掉（真实面板格子太少、测不出周期），
        # 这里改成记 OCR 命中的名字位置，用来核对锚点是否稳定。
        import ocr as _ocr
        wx, wy, ww, wh = _work_rect(frame)
        work = frame[wy:wy + wh, wx:wx + ww]
        hits = _ocr.find_keyword(work, CFG.container_keyword,
                                 CFG.ocr_lang, CFG.ocr_upscale)
        rec = {
            "t": datetime.now().isoformat(timespec="milliseconds"),
            "red_in_box": box_res["count"],
            "event": kind,
            "event_delta": delta,
            "keyword_hit": bool(hits),
            "name_bbox": [int(round(v)) for v in hits[0]["bbox"]] if hits else None,
        }
        log_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log_f.flush()

    def on_event(kind, count, delta, roi, res):
        nonlocal n_new
        ts = f"{datetime.now():%H:%M:%S}"
        if kind == "baseline":
            print(f"[{ts}] 基线建立：箱内 {count} 个红品")
            return
        if kind == "new":
            n_new += delta
            print(f"[{ts}] *** 新增红品 x{delta} ***   箱内共 {count} 个"
                  f"   本次运行累计 {n_new} 个")
            out = DEBUG_ROOT / f"box_{datetime.now():%Y%m%d_%H%M%S}_new"
            out.mkdir(parents=True, exist_ok=True)
            imwrite_u(out / "roi_raw.png", roi)
            imwrite_u(out / "roi_annotated.png", safebox.annotate(roi, res))
            imwrite_u(out / "roi_mask.png", res["mask"])
            print(f"          debug 已存: {out}")
            return
        print(f"[{ts}] 红品数减少 {count + delta} -> {count}（换局 / 清箱？）")

    try:
        safebox.monitor(g, conf["roi"], CFG, interval=args.interval,
                        on_event=on_event, on_frame=on_frame)
    except KeyboardInterrupt:
        print(f"\n已退出。本次运行共观察到 {n_new} 个新增红品。")
        if log_path:
            print(f"容器日志已写入: {log_path}")
    finally:
        if log_f:
            log_f.close()
        g.close()
    return 0


# --------------------------------------------------------------------------
def _parse_region(s: str):
    """把 "x,y,w,h" 解析成 tuple。格式不对返回 None。"""
    try:
        v = tuple(int(p) for p in s.replace(" ", "").split(","))
    except ValueError:
        return None
    return v if len(v) == 4 else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="暗区突围无限：容器 UI 检测 + 红色物品色块检测（原型）")
    p.add_argument("--window", "-w", default="", metavar="SUBSTR",
                   help="游戏窗口标题子串，如 '暗区'；留空则用当前前台窗口")
    p.add_argument("--monitor", action="store_true",
                   help="不找窗口，直接抓主显示器")
    p.add_argument("--monitor-index", type=int, default=1,
                   help="mss 显示器编号，1=主显示器（默认 1）")
    p.add_argument("--right-frac", type=float, default=None,
                   help="只看画面右侧这个比例（默认 0.40，即左侧 0.6 背包不看）")
    p.add_argument("--region", default=None, metavar="fx,fy,fw,fh",
                   help="直接指定工作区（归一化比例），跳过框选询问")
    p.add_argument("--no-region", action="store_true",
                   # 注意 %% —— argparse 的 help 会走 % 格式化，
                   # 裸的 % 会让 --help 直接抛 ValueError
                   help="忽略已保存的工作区，强制用右侧 40%% 默认值")
    p.add_argument("--no-pick", action="store_true",
                   help="启动时不询问框选（有保存值就直接用）")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("deps", help="依赖自检").set_defaults(func=cmd_deps)
    sub.add_parser("stop-all",
                   help="杀掉残留在默认端口上的服务进程（页面行为忽好忽坏时用）"
                   ).set_defaults(func=cmd_stop_all)
    sub.add_parser("list", help="列出可见窗口").set_defaults(func=cmd_list)
    sub.add_parser("snap", help="抓一帧分析并存 debug").set_defaults(func=cmd_snap)

    w = sub.add_parser("watch", help="持续运行")
    w.add_argument("--fps", type=float, default=2.0, help="轮询频率，默认 2")
    w.add_argument("--save-all", action="store_true",
                   help="每次状态变化都存 debug（默认只在红品命中时存）")
    w.add_argument("--verbose", "-v", action="store_true",
                   help="每帧都打印状态（调参时用）")
    w.set_defaults(func=cmd_watch)

    # --- Web UI ---
    sv = sub.add_parser("serve", help="起本地 Web UI（浏览器里看实时状态和时间线截图）")
    sv.add_argument("--host", default="127.0.0.1",
                    help="绑 0.0.0.0 可让手机/同网段访问（默认只本机）")
    # 数值要和 webui.DEFAULT_PORT 一致。这里不 import webui 是因为它会把
    # ocr -> winrt 一整套拉进来，而没装 WinRT 的人也应该能用 list/snap 这些命令。
    sv.add_argument("--port", type=int, default=17833,
                    help="端口，默认 17833（别用 8765，本机被 soundradar 占着）；"
                         "被占用会自动往后顺延")
    sv.add_argument("--no-browser", action="store_true", help="不要自动开浏览器")
    sv.add_argument("--interval", type=float, default=0.1,
                    help="轮询间隔秒，默认 0.1。实测单轮只要约 14ms（只抓工作区），"
                         "所以循环大部分时间在睡觉 —— 调小能直接提高采样率，"
                         "对「检出窗口很短」的情况帮助很大"
                         "判定，间隔越大判定越慢，间隔越小越不容易把两个容器并成一个")
    sv.add_argument("--miss-need", type=int, default=3,
                    help="网格连续丢失几轮才判定会话结束，默认 3")
    sv.add_argument("--ocr-interval", type=float, default=None,
                    help="OCR 最短间隔秒，默认 1.0（OCR 占整链路 76%% 的开销，"
                         "单独限频）。设 0 = 每轮都跑")
    sv.add_argument("--ocr-region", default=None, metavar="fx,fy,fw,fh",
                    help="只 OCR 这个子区域（归一化比例，相对工作区）。"
                         "跑 name 会给出算好留白的建议值")
    sv.set_defaults(func=cmd_serve)

    # --- 完整链路 ---
    tr = sub.add_parser("track",
                        help="完整链路：OCR 认保险箱 + 网格确认 + 色块检测，统计连续次数")
    tr.add_argument("--interval", type=float, default=0.4, help="轮询间隔秒，默认 0.4")
    tr.add_argument("--miss-need", type=int, default=3,
                    help="网格连续丢失几轮才判定会话结束，默认 3")
    tr.add_argument("--ocr-interval", type=float, default=None,
                    help="OCR 最短间隔秒，默认 1.0（OCR 占整链路 76%% 的开销，"
                         "单独限频）。设 0 = 每轮都跑")
    tr.add_argument("--ocr-region", default=None, metavar="fx,fy,fw,fh",
                    help="只 OCR 这个子区域（归一化比例，相对工作区）。"
                         "实测能把 OCR 从 144ms 降到 9ms。跑 name 会给出建议值")
    tr.add_argument("--verbose", "-v", action="store_true", help="每轮都打印")
    tr.set_defaults(func=cmd_track)

    sub.add_parser("select-work",
                   help="手动框选工作区（容器面板出现的区域），存起来以后一直用"
                   ).set_defaults(func=cmd_select_work)

    # --- 容器名字 OCR ---
    n = sub.add_parser("name",
                       help="OCR 右侧区域，打印识别到的文字（确认保险箱的准确写法）")
    n.add_argument("--keyword", default=None,
                   help="临时覆盖关键词（默认用 Config.container_keyword）")
    n.set_defaults(func=cmd_name)

    # --- 路线二 ---
    sub.add_parser("select-box",
                   help="交互式框选安全箱位置（只需做一次）").set_defaults(func=cmd_select_box)
    b = sub.add_parser("box", help="只监控安全箱，检测新增红品")
    b.add_argument("--interval", type=float, default=0.6,
                   help="轮询间隔秒，默认 0.6")
    b.add_argument("--log-containers", action="store_true",
                   help="旁路记录每次容器面板的格子尺寸到 JSONL，"
                        "用来判断保险箱能否靠尺寸识别（多耗约 35ms/次）")
    b.set_defaults(func=cmd_box)
    return p


CFG = Config()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.right_frac is not None:
        CFG.right_frac = args.right_frac
    if getattr(args, "keyword", None):
        CFG.container_keyword = args.keyword
    if getattr(args, "ocr_interval", None) is not None:
        CFG.ocr_interval = args.ocr_interval
    if getattr(args, "ocr_region", None):
        region = _parse_region(args.ocr_region)
        if region is None:
            print(f"--ocr-region 格式不对: {args.ocr_region!r}，应为 fx,fy,fw,fh")
            return 2
        CFG.ocr_region = region
    if args.region:
        r = _parse_region(args.region)
        if r is None or any(v > 1.0 for v in r):
            print(f"--region 格式不对: {args.region!r}，应为归一化比例 fx,fy,fw,fh"
                  f"（每项 0~1），例如 0.62,0.25,0.36,0.55")
            return 2
        CFG.work_region = r
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
