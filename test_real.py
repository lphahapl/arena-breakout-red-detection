"""
真实截图回归：用真实游戏画面验证整条链路。

这是唯一有真实数据的测试。它覆盖合成图测不出来的几件事：

  * OCR 能不能在真实画面里认出容器名字（HUD、物品名、B 站播放器 UI 全是干扰）
  * **容器名字标签的底色本身也是红的**，颜色分不开，只能靠位置和形状排除
  * **红品槽位底色是半透明的**，随背后场景明暗变化：
        整备界面(暗场景) #331410  色相 3
        对局未选中       #73312A  色相 3
        对局选中(高亮)   #9E7B70  色相 7
    所以固定色值匹配是错的方向，必须用色相 —— 这三张就是回归防线。

截图放在 tests/fixtures/real/ 下，找不到就跳过（打包版里带了三张）。

跑法:  python test_real.py
"""

from __future__ import annotations

import time

from detect import Config, find_red_blobs
from imgio import imread_u
from track import Tracker

# (文件, 说明, 期望检出的红品数)
# (文件, 说明, 期望检出的红品数)
#
# 三个红品样本覆盖了真实红品的**不同光照和不同图案占比**，缺一不可 ——
# 判据曾经因为只看了两个样本而设错两次：
#   * 亮度下限一度设 100，把 V=70 的「目标定位模块」挡掉了
#   * 实心度下限一度设 0.45，把图案占满格子的「目标定位模块」(ext=0.42) 挡掉了
# (文件, 说明, 期望红品数, 是否有容器面板)
CASES = [
    ("tests/fixtures/real/name_20260926_031955_frame.png", "整备界面 安全箱", 0, True),
    ("tests/fixtures/real/sample2_frame.png", "对局 机密保险箱（选中高亮）", 1, True),
    ("tests/fixtures/real/sample3_frame.png", "对局 机密保险箱（未选中）", 1, True),
    ("tests/fixtures/real/sample4_frame.png", "对局 保险箱（目标定位模块，图案占满整格）", 1, True),
    # sample5/6 专门守 OCR 召回率：sample5 是 1.0x 会漏掉容器名的帧，
    # sample6 是完全没有面板的帧（防多倍数 OCR 把误报放回来）。
    ("tests/fixtures/real/sample5_frame.png", "保险箱（1.0x OCR 会漏名字的帧）", 1, True),
    ("tests/fixtures/real/sample6_frame.png", "无面板（多倍数 OCR 不能误报）", 0, False),
    ("tests/fixtures/real/sample7_frame.png", "视频3 古董茶壶（大金）", 1, True),
    # sample8：红底被物品图案从中间劈成两半的帧（间距 20px）。
    # 闭运算核 9 跨不过去，会把一块真大金拆成两个不合格的碎片。
    ("tests/fixtures/real/sample8_frame.png", "视频1 t=96（红底被图案劈开）", 1, True),

]

KEYWORD = "保险箱"


def load(path):
    return imread_u(path)


def main() -> int:
    ok = True
    ran = 0

    for path, tag, want, has_panel in CASES:
        img = load(path)
        if img is None:
            print(f"[SKIP] {tag}: 找不到 {path}")
            continue
        ran += 1
        print(f"\n=== {tag}  ({path.split('/')[-1]}) ===")
        print(f"    {img.shape[1]}x{img.shape[0]}")

        cfg = Config()
        cfg.ocr_interval = 0
        cfg.ocr_interval_fast = 0
        cfg.container_keyword = KEYWORD

        tr = Tracker(cfg)
        res = tr.step(img)
        work = tr._work(img)
        excl = tr._label_exclude()

        # 1) OCR 认出容器名字（无面板的样本反过来：不能认出）
        got_panel = res["event"] == "session_start" and tr._anchor is not None
        t1 = got_panel == has_panel
        print(f"[{'PASS' if t1 else 'FAIL'}] 面板判定 {'识别到' if got_panel else '无'}"
              f"（期望{'有' if has_panel else '无'}）-> {res['event']}")
        ok &= t1
        if not got_panel:
            continue
        name = res["session"]["ocr_text"]
        print(f"       识别到 {name!r}   名字框 {tr._anchor}")
        ok &= KEYWORD in name or "保险箱" in name

        # 2) 红品判据：命中数对不对
        r = find_red_blobs(work, cfg, exclude=excl)
        hits = [b for b in r["blobs"] if b["accepted"]]
        t2 = len(hits) == want
        print(f"[{'PASS' if t2 else 'FAIL'}] 红品检出 {len(hits)} 个（期望 {want}）"
              f"   标签排除框 {excl}")
        ok &= t2

        for b in r["blobs"][:3]:
            print(f"       bbox={b['bbox']} ext={b['extent']:.2f} asp={b['aspect']:.2f} "
                  f"标签重叠={b.get('label_overlap')} "
                  f"-> {'命中' if b['accepted'] else '拒:' + ','.join(b['reject'])}")

        # 3) 标签的红底绝不能算成红品（无论这张图有没有真红品）
        label_blob = next((b for b in r["blobs"]
                           if b.get("label_overlap", 0) > 0.8), None)
        if label_blob is not None:
            t3 = not label_blob["accepted"]
            print(f"[{'PASS' if t3 else 'FAIL'}] 名字标签红底被排除"
                  f"（{label_blob['reject']}）")
            ok &= t3
        else:
            print("[SKIP] 这张图没找到标签色块")

    if ran == 0:
        print("[SKIP] 一张真实截图都没有，跳过整个用例")
        return 0

    # 4) 自适应 OCR 区域：锚定后要显著变小变快
    #    注意别写成 `load(a) or load(b)` —— numpy 数组没有真值，会抛
    #    "truth value of an array is ambiguous"
    img = load(CASES[1][0])
    if img is None:
        img = load(CASES[0][0])
    if img is not None:
        print("\n=== 自适应 OCR 区域 ===")
        cfg = Config()
        cfg.ocr_interval = 0
        cfg.ocr_interval_fast = 0
        cfg.container_keyword = KEYWORD
        tr = Tracker(cfg)
        tr.step(img)
        work = tr._work(img)
        ar = tr.anchor_region()
        total = work.shape[0] * work.shape[1]
        pct = ar[2] * ar[3] / total * 100 if ar else 0
        t4 = ar is not None and pct < 10
        print(f"[{'PASS' if t4 else 'FAIL'}] 锚定区域 {ar[2]}x{ar[3]} 占工作区 {pct:.1f}%（<10%）")
        ok &= t4

        for _ in range(3):
            tr._ocr(work)
        t0 = time.perf_counter()
        hits_fast = tr._ocr(work)
        ms_fast = (time.perf_counter() - t0) * 1000
        tr._anchor = None
        t0 = time.perf_counter()
        tr._ocr(work)
        ms_full = (time.perf_counter() - t0) * 1000
        t5 = bool(hits_fast) and ms_fast < ms_full / 2
        print(f"[{'PASS' if t5 else 'FAIL'}] 小区域 OCR 更快且仍命中: "
              f"{ms_full:.0f}ms -> {ms_fast:.0f}ms ({ms_full / max(ms_fast, 0.01):.1f}x)")
        ok &= t5

    ok &= test_panel_bounds_red_search()
    ok &= test_red_size_rule()
    ok &= test_multiscale_ocr()
    ok &= test_exclude_words()

    print("\n全部通过" if ok else "\n有失败")
    return 0 if ok else 1


def test_panel_bounds_red_search() -> bool:
    """
    红品搜索必须被限制在**容器面板范围内**。

    这条是照着一个真实故障写的：用户拿 B 站视频当输入测，场景里一块木质地板
    /暗红阴影被当成红品（实测 ext=0.47~0.56、H=8 S=165 V=49，形状判据挡不住）。
    更糟的是它在画面最底部，而存图的裁剪范围是「名字 ∪ 红品」的并集 ——
    一下把截图拉成 282x1327 的废条，什么都看不出来。

    修法：用 OCR 认出的名字位置推出面板范围（倍数相对名字高，等比缩放），
    红品只在这个范围里找。红品本来就只可能出现在容器面板里。
    """
    img = load(CASES[1][0])          # 有真红品的对局截图
    if img is None:
        print("\n[SKIP] 没有样本图，跳过「面板范围」测试")
        return True

    import numpy as np
    from detect import find_red_blobs

    cfg = Config()
    cfg.ocr_interval = 0
    cfg.ocr_interval_fast = 0
    cfg.container_keyword = KEYWORD

    tr = Tracker(cfg)
    tr.step(img)
    work = tr._work(img)
    rect = tr.panel_rect()
    if rect is None:
        print("\n[FAIL] 推不下面板范围")
        return False
    x, y, w, h = rect

    # 把容器里那块真红品的**一模一样**复制到工作区最底部 ——
    # 模拟「同样的红色出现在场景里」。颜色形状都完全合格，只有位置不对。
    r = find_red_blobs(work[y:y + h, x:x + w], cfg)
    hits = [b for b in r["blobs"] if b["accepted"]]
    if not hits:
        print("\n[FAIL] 基准都检不到红品，样本图不对")
        return False
    bx, by, bw, bh = hits[0]["bbox"]
    patch = work[y + by:y + by + bh, x + bx:x + bx + bw].copy()

    big = work.copy()
    H, W = big.shape[:2]
    px, py = 300, H - bh - 40
    big[py:py + bh, px:px + bw] = patch

    ok = True
    n_all = len([b for b in find_red_blobs(big, cfg)["blobs"] if b["accepted"]])
    t1 = n_all >= 2
    print(f"\n[{'PASS' if t1 else 'FAIL'}] 不设范围时，底部那块「场景红」会被误检 "
          f"-> 共 {n_all} 个（期望 ≥2）")
    ok &= t1

    n_bounded = len([b for b in find_red_blobs(
        big[y:y + h, x:x + w], cfg)["blobs"] if b["accepted"]])
    t2 = n_bounded == 1
    print(f"[{'PASS' if t2 else 'FAIL'}] 限定面板范围后只剩真红品 -> {n_bounded} 个（期望 1）")
    ok &= t2

    pct = h / H * 100
    t3 = pct < 45
    print(f"[{'PASS' if t3 else 'FAIL'}] 面板范围只占工作区高度 {pct:.0f}%（<45%，"
          f"场景下方进不来）")
    ok &= t3

    return ok


def test_red_size_rule() -> bool:
    """
    尺寸判据：**大红至少占两个格子**。

    这是用户给的游戏知识，也是把误报从 23 个压到 1 个的关键之一：
    只占一格甚至更小的红色区域，多半是**物品图标上的红色部件**，
    不是红品本身。

    另一条是亮度下限 V>=100 —— 容器面板是半透明的，压在亮场景上时
    会把场景的暗红底色透出来（实测 V≈46~51），而真红品 V≈120~160。
    两条合起来才够：尺寸挡小的、亮度挡大的（面板底色整片都是暗红）。

    这里验证「把真红品缩到一格大小后必须被拒」。
    """
    img = load(CASES[1][0])
    if img is None:
        print("\n[SKIP] 没有样本图，跳过「尺寸判据」测试")
        return True

    import cv2
    import numpy as np
    from detect import find_red_blobs

    cfg = Config()
    cfg.ocr_interval = 0
    cfg.ocr_interval_fast = 0
    cfg.container_keyword = KEYWORD

    tr = Tracker(cfg)
    tr.step(img)
    work = tr._work(img)
    x, y, w, h = tr.panel_rect()
    name_h = tr._anchor[3]

    def detect(region_bgr):
        return [b for b in find_red_blobs(region_bgr, cfg, name_h=name_h)["blobs"]
                if b["accepted"]]

    full = detect(work[y:y + h, x:x + w])
    if not full:
        print("\n[FAIL] 基准都检不到红品，样本图不对")
        return False

    bx, by, bw, bh = full[0]["bbox"]
    ok = True

    # 把红品格子缩成**一格大小**（宽高各砍一半）-> 必须被拒
    cell = name_h * cfg.cell_from_name_h
    sub = work[y:y + h, x:x + w].copy()
    small = sub[by:by + bh, bx:bx + bw].copy()
    sub[by:by + bh, bx:bx + bw] = 40        # 先抹掉原来的
    sh, sw = int(cell * 0.8), int(cell * 0.8)
    sub[by:by + sh, bx:bx + sw] = cv2.resize(small, (sw, sh))

    one_cell = detect(sub)
    t1 = len(one_cell) == 0
    print(f"\n[{'PASS' if t1 else 'FAIL'}] 缩到一格大小的红块被拒 -> "
          f"检出 {len(one_cell)} 个（期望 0，一格 ≈{cell:.0f}px）")
    ok &= t1

    # 保持两格大小 -> 仍然检出
    sub2 = work[y:y + h, x:x + w].copy()
    t2 = len(detect(sub2)) >= 1
    print(f"[{'PASS' if t2 else 'FAIL'}] 两格大小的红块仍检出 -> {len(detect(sub2))} 个")
    ok &= t2

    return ok


def test_multiscale_ocr() -> bool:
    """
    OCR 必须跑多个放大倍数取并集 —— 单跑任一遍都会漏掉一类容器名。

    这是用户报的「出红没有探测到」的**根因**：漏掉容器名 -> 根本不开会话 ->
    后面红品判据再准也没机会跑。实测两边盲区互补：
        1.0x 漏「保险箱」   （sample5 那一帧）
        1.5x 漏「机密保险箱」（sample2/sample3）
    并集在 7 正样本上 7/7、7 负样本上 0 误报。
    """
    import cv2
    from detect import Config as C
    import ocr as ocr_mod

    img = load("tests/fixtures/real/sample5_frame.png")
    if img is None:
        print("[SKIP] 没有 sample5，跳过「多倍数 OCR」测试")
        return True

    work = img[:, int(img.shape[1] * 0.6):]
    ok = True

    def hit_at(sc):
        im = work if sc == 1.0 else cv2.resize(work, None, fx=sc, fy=sc,
                                               interpolation=cv2.INTER_CUBIC)
        return any("保险箱" in ocr_mod.flatten(l["text"])
                   for l in ocr_mod.recognize(im))

    only10 = hit_at(1.0)
    both = hit_at(1.0) or hit_at(1.5)
    t1 = both
    print(f"[{'PASS' if t1 else 'FAIL'}] 多倍数并集能认出这一帧的名字"
          f"（单跑 1.0x {'也能' if only10 else '会漏'}）")
    ok &= t1

    t2 = not only10
    print(f"[{'PASS' if t2 else 'FAIL'}] 单跑 1.0x 确实会漏 -> {only10}"
          f"（若不漏说明这帧不再是有效样本，换一帧）")
    ok &= t2

    # 无面板的帧不能被误报
    neg = load("tests/fixtures/real/sample6_frame.png")
    if neg is not None:
        tr = Tracker(C())
        tr.cfg.container_keyword = KEYWORD
        tr.cfg.ocr_interval = 0
        res = tr.step(neg)
        t3 = res["event"] != "session_start"
        print(f"[{'PASS' if t3 else 'FAIL'}] 无面板的帧不被误报 -> {res['event']}")
        ok &= t3
    return ok


def test_exclude_words() -> bool:
    """
    物品名排除词：**图案本身是红色**的物品要靠 OCR 名字挡掉。

    这是用户提的思路，实测有效：「行星之子」的红来自它的图案（红色星球），
    不是槽位底色，纯颜色/尺寸判据完全挡不住。但它的名字里有「行星」，
    对红块那一小块 OCR 就能读出来。

    注意 sample9 是**面板裁剪图**不是整帧，所以不能走主用例循环
    （Tracker 在裁剪图里找不到容器名 —— 名字在裁剪图左侧、落在工作区外）。
    """
    img = load("tests/fixtures/real/sample9_frame.png")
    if img is None:
        print("[SKIP] 没有 sample9，跳过「排除词」测试")
        return True

    import cv2
    from detect import Config as C, find_red_blobs
    import ocr as ocr_mod

    cfg = C()
    cfg.ocr_interval = 0
    cfg.ocr_upscales = (1.0,)

    ok = True
    # 1) 没开排除词时，它**会**被当成红品（说明纯颜色/尺寸挡不住）
    blobs = find_red_blobs(img, cfg, name_h=23)
    n_before = len([b for b in blobs["blobs"] if b["accepted"]])

    # 2) 走一遍名字过滤，应当被排除
    tr = Tracker(cfg)
    kept = tr._filter_by_text(img, [b for b in blobs["blobs"] if b["accepted"]])

    texts = [b.get("ocr_text", "") for b in blobs["blobs"] if b["accepted"]]
    t1 = n_before >= 1
    print(f"[{'PASS' if t1 else 'FAIL'}] 不开排除词时它会被当成红品 -> {n_before} 个"
          f"（证明颜色/尺寸判据挡不住这类）")
    ok &= t1

    t2 = len(kept) == 0
    print(f"[{'PASS' if t2 else 'FAIL'}] 加物品名过滤后被排除 -> 剩 {len(kept)} 个"
          f"  OCR读到: {texts!r}")
    ok &= t2

    return ok


if __name__ == "__main__":
    raise SystemExit(main())
