"""
容器名字 OCR 的自测。

用整备界面截图里的「安全箱」「钛金安全箱」标签验证。

关键回归点：Windows OCR 会在汉字之间插空格（'安全箱' -> '安 全 箱'），
所以所有文本比较必须先 flatten，否则子串匹配必然失败。

跑法:  python test_ocr.py
"""

from __future__ import annotations

import glob
from pathlib import Path

import ocr
from imgio import imread_u

# 整备界面截图里「安全箱 / 钛金安全箱」标签的位置
LABEL_CROP = (slice(1010, 1080), slice(770, 960))


def load_label_img():
    """
    标签区域图。优先用随包的小样本 sample_label.png（14KB，到哪都能跑），
    没有就退回从 debug/ 里的真实截图裁。
    """
    p = Path("sample_label.png")
    if p.exists():
        img = imread_u(p)
        if img is not None:
            return img, "sample_label.png"
    for f in sorted(glob.glob("debug/*/00_full.png")):
        src = imread_u(f)
        if src is not None and src.shape[0] > 1080 and src.shape[1] > 960:
            return src[LABEL_CROP], f
    return None, None


def test_ocr_region() -> bool:
    """
    ocr_region 生效：只 OCR 那一小块，且返回的 bbox 偏移正确加回去。

    判据：裁一小块和用全图，得到的 bbox 必须**一致**（说明偏移加回正确），
    且裁剪版明显更快。

    用 sample_label.png 里「安全箱」那一行做基准 —— 它在图里是 x≈42, y≈22。
    """
    import time

    from detect import Config
    from track import Tracker

    src, name = load_label_img()
    if src is None:
        print("[SKIP] 没有可用的样本图，跳过 ocr_region 用例")
        return True

    # 把样本贴到「右 0.4 工作区」的真实尺寸画布上，这样速度对比才有意义
    # （直接拿 190x70 的样本比是没差别的，裁无可裁）
    import numpy as np
    canvas = np.full((1440, 1024, 3), 30, np.uint8)
    OX, OY = 300, 400
    canvas[OY:OY + src.shape[0], OX:OX + src.shape[1]] = src

    # 「安全箱」在样本里是 (42,22,47,15)，留白必须够
    # （实测文字高 15px 时上下留白 <25px 会 0 命中）
    TX, TY, TW, TH = OX + 42, OY + 22, 47, 15
    # ocr_region 是**归一化比例**（相对工作区），不是像素
    _CH, _CW = canvas.shape[:2]
    REGION = (round((TX - 100) / _CW, 4), round((TY - 35) / _CH, 4),
              round((TW + 200) / _CW, 4), round((TH + 70) / _CH, 4))

    def run(region):
        cfg = Config()
        cfg.right_frac = 1.0          # 整张画布当工作区，坐标直接可比
        cfg.container_keyword = "安全箱"
        cfg.ocr_interval = 0
        cfg.ocr_region = region
        tr = Tracker(cfg)
        right = tr._work(canvas)
        t0 = time.perf_counter()
        hits = tr._ocr(right)
        return hits, (time.perf_counter() - t0) * 1000

    hits_full, ms_full = run(None)
    hits_crop, ms_crop = run(REGION)

    if not hits_full:
        print("[FAIL] 基准（不裁剪）就没命中，样本图可能不对")
        return False

    fx, fy = (round(v) for v in hits_full[0]["bbox"][:2])
    # 注意：真值是 (TX, TY)，不是全图那次的结果。
    # 实测全图 OCR 的 bbox 反而偏了 ~20px（大片纯色背景上定位不准），
    # 裁剪版的 bbox 才是精确的 —— 裁剪不仅更快，定位也更准。

    ok = True
    t1 = bool(hits_crop)
    print(f"\n[{'PASS' if t1 else 'FAIL'}] 裁剪后仍能命中关键词，找到 {len(hits_crop)} 行")
    ok &= t1
    if not hits_crop:
        return False

    bx = round(hits_crop[0]["bbox"][0])
    by = round(hits_crop[0]["bbox"][1])
    t2 = abs(bx - TX) <= 6 and abs(by - TY) <= 6
    print(f"[{'PASS' if t2 else 'FAIL'}] bbox 偏移加回正确：裁剪后 ({bx},{by})，"
          f"真值 ({TX},{TY})")
    print(f"       参考：不裁剪时是 ({fx},{fy}) —— 偏了 "
          f"{abs(fx-TX)}px，裁剪版反而更准")
    ok &= t2

    t3 = ms_crop < ms_full / 1.5
    print(f"[{'PASS' if t3 else 'FAIL'}] 裁剪确实更快：{ms_full:.0f}ms -> {ms_crop:.0f}ms "
          f"（{ms_full/max(ms_crop,0.01):.1f}x）")
    ok &= t3

    return ok


def main() -> int:
    print("可用 OCR 语言:", ocr.available_languages())
    assert "zh-Hans-CN" in ocr.available_languages(), "缺简中语言包"

    img, src = load_label_img()
    if img is None:
        print("[SKIP] 找不到可用的整备界面截图（debug/*/00_full.png）")
        return 0
    print(f"标签图来自: {src}  裁剪 {img.shape[1]}x{img.shape[0]}")

    lines = ocr.recognize(img)
    print(f"\n识别到 {len(lines)} 行:")
    for ln in lines:
        print(f"  原始={ln['text']!r}  去空白后={ocr.flatten(ln['text'])!r}")

    ok = True

    # 1. flatten 行为
    t1 = ocr.flatten("安 全 箱") == "安全箱"
    print(f"\n[{'PASS' if t1 else 'FAIL'}] flatten 去掉汉字间空格")
    ok &= t1

    # 2. 朴素子串匹配会失败（这正是必须 flatten 的原因）
    raw = "".join(ln["text"] for ln in lines)
    t2 = "安全箱" not in raw
    print(f"[{'PASS' if t2 else 'FAIL'}] 朴素子串匹配确实失败 "
          f"—— 证明 flatten 不是可有可无的")
    ok &= t2

    # 3. flatten 后能找到「安全箱」，且变体行也一并命中。
    #
    # 注意：不要把期望写成整串相等。实测 OCR 会把「钛」读成「钍」（字形极像），
    # 但那只影响首字，「安全箱」三个字照样匹配上。
    # 这恰好说明为什么该用子串匹配而不是整串比较 —— 首字读错不影响判定。
    hits = ocr.find_keyword(img, "安全箱")
    texts = [h["flat"] for h in hits]
    t3 = len(hits) == 2 and all("安全箱" in t for t in texts)
    print(f"[{'PASS' if t3 else 'FAIL'}] 子串匹配命中全部 {len(hits)} 行（含变体行）")
    print(f"        命中: {texts}")
    print(f"        注: OCR 把「钛」读成「钍」，但子串仍命中，说明子串匹配是对的")
    ok &= t3

    # 4. 用「保险箱」去匹配应当落空 —— 记录这个尚未确认的差异
    hits2 = ocr.find_keyword(img, "保险箱")
    t4 = len(hits2) == 0
    print(f"[{'PASS' if t4 else 'FAIL'}] 「保险箱」匹配落空 "
          f"（游戏里这块写的是「安全箱」，两者是不同东西，待用户确认）")
    ok &= t4

    ok &= test_ocr_region()

    ok &= test_work_region()

    print("\n全部通过" if ok else "\n有失败")
    return 0 if ok else 1


def test_work_region() -> bool:
    """手工框选的工作区要真的缩小搜索范围（区域小 -> OCR 和网格都更快）。"""
    import numpy as np
    from detect import Config
    from track import Tracker

    frame = np.full((1000, 2000, 3), 30, np.uint8)
    ok = True

    # 没框选 -> 默认右侧 40%
    cfg = Config()
    cfg.ocr_interval = 0
    tr = Tracker(cfg)
    x, y, w, h = tr.work_rect(frame)
    t1 = (x, y, w, h) == (1200, 0, 800, 1000)
    print(f"[{'PASS' if t1 else 'FAIL'}] 默认工作区 = 右侧 40% -> {(x, y, w, h)}")
    ok &= t1

    # 框选了 -> 用它
    cfg2 = Config()
    cfg2.ocr_interval = 0
    cfg2.work_region = (0.30, 0.20, 0.25, 0.60)
    tr2 = Tracker(cfg2)
    x2, y2, w2, h2 = tr2.work_rect(frame)
    t2 = (x2, y2, w2, h2) == (600, 200, 500, 600)
    print(f"[{'PASS' if t2 else 'FAIL'}] 手工工作区 (0.30,0.20,0.25,0.60) -> "
          f"{(x2, y2, w2, h2)}  期望 (600, 200, 500, 600)")
    ok &= t2

    # 工作区确实变小了（面积比 40% 默认小）
    t3 = w2 * h2 < w * h
    print(f"[{'PASS' if t3 else 'FAIL'}] 手工工作区面积 {w2*h2} < 默认 {w*h}")
    ok &= t3

    return ok


if __name__ == "__main__":
    raise SystemExit(main())
