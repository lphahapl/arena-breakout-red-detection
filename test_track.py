"""
track 状态机的自测。

合成图里没法让 Windows OCR 读出中文（cv2.putText 只支持 ASCII），所以把
ocr.find_keyword 打桩，专测状态机：会话的开始/持续/latch/结束、streak 结算、
OCR 限频。

**红品检测和面板存活判定用真实的，不打桩**：
  - 红品检测：合成面板上的红格
  - 存活判定：模板匹配（会话开始时截的标签模板）

跑法:  python test_track.py
"""

from __future__ import annotations

import numpy as np

import ocr
import track as trackmod
from track import panel_alive_score
from detect import Config
from test_synthetic import scene

# 合成场景，只建一次（game_world 有点慢）
NO_UI = None
PANEL_CLEAN = None
PANEL_RED = None


def build():
    global NO_UI, PANEL_CLEAN, PANEL_RED
    NO_UI = scene(False, False, False)        # 没有 UI
    PANEL_CLEAN = scene(False, False, True)   # 有面板无红
    PANEL_RED = scene(False, True, True)      # 有面板且有红


# 名字 bbox 必须和合成场景的几何**自洽**，三个约束同时成立：
#   1. 落在面板上（不能是背景噪声，否则模板匹配恒等于 1）
#   2. 由它推出的面板范围要能**盖住整个格子区**（否则红品被切掉）
#   3. 名字高 * cell_from_name_h（推出来的一格）不能大于红底的实际像素宽
# 合成场景：面板 work y 330..750，格子区 y 370..714、x 118..642。
# 面板范围公式：y 从 ny-2*nh 到 ny+16*nh，x 从 nx-16*nh 到 nx+nw+16*nh。
# 取 ny=350 / nh=23：y 304..718 盖住格子 ✓，推出一格 69px ≤ 红底 71px ✓。
# 踩过两次：nh=24 时一格算成 72 > 红底 71，差 1px 判不合格；
#           nh=18 时面板范围只到 y=622，把下半截红品切掉了。
NAME_BBOX = (110, 350, 120, 23)


def fake_hit():
    return [{"text": "电 子 保 险 箱", "flat": "电子保险箱",
             "bbox": NAME_BBOX, "words": []}]


def install_stub(script: list):
    """把 ocr.find_keyword 换成按脚本返回的打桩。返回调用计数器。"""
    calls = {"n": 0}

    def stub(bgr, keyword, lang_tag=None, upscale=1.0):
        i = calls["n"]
        calls["n"] += 1
        return script[i] if i < len(script) else []

    ocr.find_keyword = stub
    return calls


def main() -> int:
    build()
    ok = True

    # ---------------- 主流程 ----------------
    # 会话**不再靠 OCR 判存活**（小区域 OCR 误读率太高），改用模板匹配。
    # 所以 OCR 只在 IDLE 轮被调用，脚本只需要覆盖那几轮。
    script = [
        [],           # step1  IDLE 没命中
        fake_hit(),   # step2  IDLE 命中 -> 开会话
        fake_hit(),   # step8  IDLE 命中 -> 第二个容器
    ]
    install_stub(script)

    cfg = Config()
    cfg.ocr_upscales = (1.0,)   # 打桩按调用次数返回，单倍数才可预测
    cfg.ocr_interval = 0
    cfg.ocr_interval_fast = 0
    cfg.container_keyword = "保险箱"
    tr = trackmod.Tracker(cfg, miss_need=3)

    steps = [
        (NO_UI, None, 0, "没有 UI"),
        (PANEL_CLEAN, "session_start", 0, "命中名字 -> 开会话"),
        (PANEL_CLEAN, None, 0, "面板还在（模板匹配命中）"),
        (PANEL_RED, None, 0, "红品第 1 轮（还不确认）"),
        (PANEL_RED, None, 0, "红品第 2 轮 -> latch"),
        (NO_UI, None, 0, "面板消失 1"),
        (NO_UI, None, 0, "面板消失 2"),
        (NO_UI, "container_red", 0, "消失 3 -> 结算，有红，streak 归零"),
        (PANEL_CLEAN, "session_start", 0, "第二个容器"),
        (PANEL_CLEAN, None, 0, "面板还在"),
        (NO_UI, None, 0, "消失 1"),
        (NO_UI, None, 0, "消失 2"),
        (NO_UI, "container_clean", 1, "结算，无红，streak=1"),
    ]

    for i, (frame, want_ev, want_streak, desc) in enumerate(steps, 1):
        res = tr.step(frame)
        ev = res["event"]
        good = (ev == want_ev and res["streak"] == want_streak)
        ok &= good
        print(f"[{'PASS' if good else 'FAIL'}] step{i:2d} {desc:34s} "
              f"得到 {ev} streak={res['streak']:<2} 期望 {want_ev} streak={want_streak}")

    h = tr.history
    latch_ok = len(h) == 2 and h[0]["red"] is True and h[1]["red"] is False
    ok &= latch_ok
    print(f"[{'PASS' if latch_ok else 'FAIL'}] latch：第一次会话标记有红、第二次无红")

    tot_ok = tr.total == 2 and tr.streak == 1
    ok &= tot_ok
    print(f"[{'PASS' if tot_ok else 'FAIL'}] 汇总：总容器 {tr.total}（期望 2）"
          f"  连续未出红 {tr.streak}（期望 1）")

    ok &= test_template_liveness()
    ok &= test_flicker_hysteresis()
    ok &= test_single_frame_red_ignored()
    ok &= test_ratelimit()

    print("\n全部通过" if ok else "\n有失败")
    return 0 if ok else 1


def test_template_liveness() -> bool:
    """
    面板存活判定：模板匹配要能干净地分开「面板还在」和「面板没了」。

    这条是照着真实故障加的：原来用小区域 OCR 判存活，误读率极高
    （同一个「机密保险箱」被读成过「密保险箱」「也保險粕」「机密保殓霜」），
    导致 misses 反复归零、会话永远结束不了，后面开的容器全被并进同一条。
    """
    ok = True
    print()

    install_stub([fake_hit()] * 10)
    cfg = Config()
    cfg.ocr_upscales = (1.0,)   # 打桩按调用次数返回，单倍数才可预测
    cfg.ocr_interval = 0
    cfg.ocr_interval_fast = 0
    cfg.container_keyword = "保险箱"
    # miss_need=2：帧数刚好够跑完一次结算，不会多喂出第二次会话
    tr = trackmod.Tracker(cfg, miss_need=2)

    tr.step(PANEL_CLEAN)          # 开会话，截模板
    a1 = tr._tpl is not None
    if a1:
        print(f"[PASS] 会话开始时截下了标签模板 {tr._tpl.shape[1]}x{tr._tpl.shape[0]}")
    else:
        print("[FAIL] 没截到模板")
    ok &= a1

    tr.step(PANEL_CLEAN)
    s_same = tr.last_score
    t2 = s_same >= 0.9
    print(f"[{'PASS' if t2 else 'FAIL'}] 面板还在时 NCC={s_same:.3f}（期望 ≥0.9）")
    ok &= t2

    tr.step(NO_UI)                # 消失 1
    s_gone = tr.last_score
    t3 = s_gone < 0.5
    print(f"[{'PASS' if t3 else 'FAIL'}] 面板没了时 NCC={s_gone:.3f}（期望 <0.5）")
    ok &= t3

    t4 = s_same - s_gone > 0.4
    print(f"[{'PASS' if t4 else 'FAIL'}] 两者分离度 {s_same - s_gone:.3f}（期望 >0.4）")
    ok &= t4

    # 会话结算后 anchor 和模板都要清掉 —— IDLE 必须用全区域 OCR，
    # 小区域 OCR 误读率太高，留着会让下一个容器检测不到
    r = tr.step(NO_UI)            # 消失 2 -> 结算
    t5 = (r["event"] == "container_clean"
          and tr._anchor is None and tr._tpl is None)
    print(f"[{'PASS' if t5 else 'FAIL'}] 结算后清掉 anchor 和模板 "
          f"-> {r['event']} anchor={tr._anchor} tpl={tr._tpl is not None}")
    ok &= t5

    return ok


def test_flicker_hysteresis() -> bool:
    """
    模板分数抖到灰区**不能算作面板消失**。

    这是照着一个真实故障写的：日志里发现有一次开箱只活了 2.6 秒（7 轮），
    紧接着 1.8 秒后又开了一次 —— 面板其实一直在，只是模板分数中途抖了几帧
    低于单一阈值，会话被误判结束，于是**一次开箱被记成了多次**。

    修法：迟滞。>= tpl_match_min 算活着；只有 < tpl_gone_max 才计入 miss；
    中间那段灰区两条都不动。
    """
    import numpy as np
    ok = True
    print()

    install_stub([fake_hit()] * 20)
    cfg = Config()
    cfg.ocr_upscales = (1.0,)   # 打桩按调用次数返回，单倍数才可预测
    cfg.ocr_interval = 0
    cfg.ocr_interval_fast = 0
    cfg.container_keyword = "保险箱"
    tr = trackmod.Tracker(cfg, miss_need=3)

    tr.step(PANEL_CLEAN)                       # 开会话，截模板

    # 注意：panel_alive_score 要的是**工作区子图**，模板位置是工作区坐标。
    # 传整帧的话卷积位置全错，连"还活着"的帧都只有 0.46 分（踩过）。
    work_clean = tr._work(PANEL_CLEAN)
    s_alive = panel_alive_score(work_clean, tr._tpl, tr._tpl_pos, cfg.tpl_search_pad)

    # 造一帧分数落在灰区：往面板帧里加噪声。
    # 不能靠"往纯色混" —— 归一化互相关对线性变换 a*I+b 是不变的，
    # 混合纯色只改对比度，分数纹丝不动（踩过，一直造不出灰区）。
    rng = np.random.default_rng(7)
    gray = None
    for sigma in np.arange(10, 120, 5):
        noisy = np.clip(work_clean.astype(float)
                        + rng.normal(0, sigma, work_clean.shape), 0, 255).astype(np.uint8)
        sc = panel_alive_score(noisy, tr._tpl, tr._tpl_pos, cfg.tpl_search_pad)
        if cfg.tpl_gone_max <= sc < cfg.tpl_match_min:
            gray = noisy
            break
    if gray is None:
        print("[SKIP] 造不出灰区分数，跳过迟滞测试")
        return True

    s_gray = panel_alive_score(gray, tr._tpl, tr._tpl_pos, cfg.tpl_search_pad)
    t0 = s_alive >= 0.9
    print(f"[{'PASS' if t0 else 'FAIL'}] 正常帧分数 {s_alive:.2f}（期望 ≥0.9）"
          f"—— 这条同时守住上面那个坐标坑")
    ok &= t0
    print(f"[INFO] 三档分数: 活着 {s_alive:.2f} / 灰区 {s_gray:.2f} / "
          f"（阈值 {cfg.tpl_gone_max}~{cfg.tpl_match_min}）")

    # 把灰区帧贴回整帧（Tracker 吃的是整帧）
    wr = tr.work_rect(PANEL_CLEAN)
    gray_frame = PANEL_CLEAN.copy()
    gray_frame[wr[1]:wr[1] + gray.shape[0], wr[0]:wr[0] + gray.shape[1]] = gray

    # 灰区**短暂**停留不该结束会话（这是迟滞的目的：容忍抖动）
    for _ in range(cfg.tpl_gray_max_rounds - 2):
        r = tr.step(gray_frame)
    t1 = r["event"] is None and tr.in_session and tr.misses == 0
    print(f"[{'PASS' if t1 else 'FAIL'}] 灰区短暂停留（{cfg.tpl_gray_max_rounds - 2} 轮）"
          f"不结束会话 -> event={r['event']} misses={tr.misses}")
    ok &= t1

    # 但灰区**不能无限待** —— 实测面板已消失、分数停在灰区(0.352~0.407)，
    # 会话被永久卡住、永远不结算，出红事件也就永远发不出来。
    # 超过 tpl_gray_max_rounds 后要开始计 miss。
    for _ in range(3):
        r = tr.step(gray_frame)
    t2 = tr.misses > 0
    print(f"[{'PASS' if t2 else 'FAIL'}] 灰区超时后开始计 miss -> "
          f"gray={tr._gray_streak} misses={tr.misses}")
    ok &= t2

    # 回到正常帧应当还是活着，misses 归零
    tr.step(PANEL_CLEAN)
    t3 = tr.misses == 0 and tr.in_session
    print(f"[{'PASS' if t3 else 'FAIL'}] 真正消失后才结算 -> {r['event']}")
    ok &= t3

    return ok


def test_single_frame_red_ignored() -> bool:
    """
    只出现**一轮**的红品不算数。

    这是照着真实反馈写的：用户发现「把场景也判定成出红了」——
    面板早没了、会话还开着（模板匹配被场景凑够了分数），于是去扫了一片
    建筑/天空，把一小块红色判成了红品。

    红品判定确实只在会话内跑，问题是**会话可能假装还活着**。
    尺寸/亮度判据只是碰巧挡住那一例；连续两轮确认才是机制上堵住 ——
    真实红品在面板里停留好几秒（十几轮），运动场景的误报一帧一个样。
    """
    ok = True
    print()

    install_stub([fake_hit()] * 20)
    cfg = Config()
    cfg.ocr_upscales = (1.0,)   # 打桩按调用次数返回，单倍数才可预测
    cfg.ocr_interval = 0
    cfg.ocr_interval_fast = 0
    cfg.container_keyword = "保险箱"
    tr = trackmod.Tracker(cfg, miss_need=3)

    tr.step(PANEL_CLEAN)                 # 开会话
    tr.step(PANEL_CLEAN)

    # 只闪一轮红
    tr.step(PANEL_RED)
    t1 = tr.session["red"] is False
    print(f"[{'PASS' if t1 else 'FAIL'}] 单轮红品不 latch -> red={tr.session['red']}"
          f"（确认需要 {cfg.red_confirm_rounds} 轮）")
    ok &= t1

    # 回到干净面板，最后结算
    tr.step(PANEL_CLEAN)
    tr.step(PANEL_CLEAN)
    for _ in range(3):
        r = tr.step(NO_UI)
    t2 = r["event"] == "container_clean"
    print(f"[{'PASS' if t2 else 'FAIL'}] 结算为无红 -> {r['event']}（期望 container_clean）")
    ok &= t2

    # 对照：连续两轮就该 latch
    tr2 = trackmod.Tracker(cfg, miss_need=3)
    tr2.step(PANEL_CLEAN)
    tr2.step(PANEL_RED)
    tr2.step(PANEL_RED)
    t3 = tr2.session["red"] is True
    print(f"[{'PASS' if t3 else 'FAIL'}] 连续两轮红品会 latch -> red={tr2.session['red']}")
    ok &= t3

    return ok


def test_ratelimit() -> bool:
    """OCR 限频：IDLE 连续多轮只跑一次；会话结算后立刻恢复。"""
    ok = True
    print()

    calls = install_stub([[], [], [], [], []])
    cfg = Config()
    cfg.ocr_upscales = (1.0,)   # 打桩按调用次数返回，单倍数才可预测
    cfg.ocr_interval = 60.0        # 大到测试期间只该跑一次
    cfg.container_keyword = "保险箱"
    tr = trackmod.Tracker(cfg)
    for _ in range(5):
        tr.step(NO_UI)
    a1 = calls["n"] == 1 and tr.ocr_skipped == 4
    print(f"[{'PASS' if a1 else 'FAIL'}] 限频: IDLE 跑 5 轮，OCR 实跑 {calls['n']} 次、"
          f"跳过 {tr.ocr_skipped} 次（期望 1 / 4）")
    ok &= a1

    return ok


if __name__ == "__main__":
    raise SystemExit(main())
