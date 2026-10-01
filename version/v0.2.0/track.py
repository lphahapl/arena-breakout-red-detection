"""
完整链路：OCR 认容器名字 -> 锚定面板 -> 面板内色块检测 -> 统计连续次数。

## 为什么不用「大量重复格子」判断 UI 了

原设计里有一层「网格检测」（边缘密度 -> 连通域 -> 自相关测格子间距）。
拿真实截图实测后**这条被拿掉了**，原因：

  1. 容器面板格子很少。实测「电子保险箱」面板是 2x2、间距约 87px ——
     格子太少，投影太短，自相关根本形不成显著峰。
  2. 格子里的物品图案纹理极丰富（集邮册那一格全是细节），边缘投影被
     内容边缘淹没，周期性信号被压掉。
  3. 它本来就只是**弱判据**：枪皮花纹、地砖、天花板格栅都会误命中（实测撞到过）。

而 OCR 是强判据，还能顺便给出容器名字的**精确 bbox**，直接用它锚定面板就够了。

## 自适应 OCR 区域

第一次 OCR 扫整个工作区（实测 300ms，因为游戏画面里还有 HUD 和物品名要一起识别）。
拿到名字 bbox 后就只扫名字周围那一小块（~9ms），之后一直用它。
连续几次扫不到就退回全区域重新找 —— 这样换一种容器、名字位置变了也能自愈。

## 会话式

搜索要好几秒，物品是延迟之后才逐个出现的，所以「匹配上」不是一帧的事：

    IDLE      OCR 命中 -> 记录名字位置 -> 进入 SESSION
    SESSION   每轮：OCR 小区域确认面板还在 + 全区域色块检测（红品 latch）
              OCR 连续 N 轮不命中 -> 结束，结算
    结算      有红 -> streak 归零；没红 -> streak += 1

红品必须 latch：搜刮时物品逐个出现，红品可能只在某几帧可见，随后被别的物品盖住。

## 一个实测确认过的坑

容器名字标签的**背景本身就是红的**，而且会通过 R/G、R/B 判定
（实测标签右侧底 #4D1D1A，R/G=2.66；上边框 #341716，和红品 #331410 几乎一样）。

好在核心匹配率把它们挡住了：标签底色的实测 core_match 只有 0.005
（真实红品是 0.831），而那条 #341716 只有 1~2px 宽，腐蚀后没有核心像素。
所以**红品检测不需要为标签做特殊处理** —— 但这条依赖 core_match 判据，
改参数时别把它调松。
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime

import numpy as np

import cv2

from detect import Config, find_red_blobs
import ocr


def _bbox_iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * \
        max(0, min(ay + ah, by + bh) - max(ay, by))
    union = aw * ah + bw * bh - overlap
    return overlap / union if union else 0.0


def panel_alive_score(work: np.ndarray, tpl: np.ndarray | None,
                      tpl_pos: tuple[int, int], search_pad: int) -> float:
    """
    容器面板还在不在 —— 返回归一化互相关分数（1.0 = 一模一样，0 = 完全没了）。

    没有模板时返回 1.0（当作还活着），这样退化成「只会自然结束」而不是乱结束。
    """
    if tpl is None or tpl.size == 0:
        return 1.0
    th, tw = tpl.shape[:2]
    x0, y0 = tpl_pos
    sx0, sy0 = max(x0 - search_pad, 0), max(y0 - search_pad, 0)
    sx1 = min(x0 + tw + search_pad, work.shape[1])
    sy1 = min(y0 + th + search_pad, work.shape[0])
    search = work[sy0:sy1, sx0:sx1]
    if search.shape[0] < th or search.shape[1] < tw:
        return 0.0
    r = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
    return float(r.max())


class Tracker:
    """把「一轮观察」变成「容器会话」的状态机。"""

    def __init__(self, cfg: Config, miss_need: int = 3):
        self.cfg = cfg
        self.miss_need = miss_need
        self.in_session = False
        self.misses = 0
        self.session: dict | None = None
        self.streak = 0
        self.total = 0
        self.history: list[dict] = []
        self.ocr_ms_total = 0.0
        self.ocr_calls = 0
        self.ocr_skipped = 0
        self.ocr_scales_run = 0          # 实际跑了几遍放大倍数（看惰性省了多少）
        self.text_cache_hits = 0         # _filter_by_text 的缓存命中次数
        self.item_ocr_calls = 0
        self.item_ocr_ms_total = 0.0
        self.last_candidates: list[dict] = []
        self._last_ocr_t = 0.0
        self._last_full_ocr_t = 0.0
        self._idle_anchor: tuple | None = None
        self._work_shape: tuple | None = None
        # 逐块物品名 OCR 的记忆。键是裁剪内容的哈希 —— 搜刮时面板画面大部分
        # 轮次不变，命中缓存后这一整段（实测占该轮 80%）就没了。
        self._text_cache: dict[bytes, tuple[str, str]] = {}
        # 上次 OCR 命中的名字 bbox（工作区坐标）。有它就只扫这一小块。
        self._anchor: tuple | None = None
        # 会话开始时截下的名字标签模板 + 它在工作区里的位置。
        # 会话内靠模板匹配判「面板还在不在」，比 OCR 可靠得多。
        # 最近几轮的红品命中情况（滑动窗口）。用"最近 N 轮里中 M 轮"而不是
        # "连续 N 轮" —— 实测视频里红品可能只被检出单独一轮，连续要求会漏。
        self._red_hist: list[list[tuple]] = []
        # 连续待在灰区的轮数。灰区只能短暂停留，待久了当成消失（见下）
        self._gray_streak = 0
        self._tpl: np.ndarray | None = None
        self._tpl_pos: tuple[int, int] = (0, 0)
        self.last_score: float = 0.0

    # -- 工作区 ------------------------------------------------------------
    def work_rect(self, frame: np.ndarray) -> tuple[int, int, int, int]:
        """工作区在整帧里的像素矩形。有手动画的就用它，否则退回 right_frac。"""
        h, w = frame.shape[:2]
        if self.cfg.frame_is_work:
            return (0, 0, w, h)
        wr = self.cfg.work_region
        if wr:
            from roi import to_pixels
            return to_pixels(tuple(wr), frame.shape)
        x0 = int(round(w * (1.0 - self.cfg.right_frac)))
        return (x0, 0, max(w - x0, 1), h)

    def _work(self, frame: np.ndarray) -> np.ndarray:
        x, y, w, h = self.work_rect(frame)
        return frame[y:y + h, x:x + w]

    # -- OCR ---------------------------------------------------------------
    def anchor_region(self) -> tuple[int, int, int, int] | None:
        """
        名字周围的小区域。留白按文字高度算 —— 实测 Windows OCR 对裁剪留白
        极敏感，文字高 21px 时上下留白 <35px 会直接返回空。
        """
        anchor = self._anchor or (self._idle_anchor if not self.in_session else None)
        if not anchor:
            return None
        nx, ny, nw, nh = anchor
        pad_x = max(100, nw)
        pad_y = max(35, int(nh * 2.5))
        return (max(nx - pad_x, 0), max(ny - pad_y, 0), nw + pad_x * 2, nh + pad_y * 2)

    def panel_rect(self) -> tuple[int, int, int, int] | None:
        """
        容器面板的搜索范围 —— 由 OCR 认出的名字位置推出来。

        红品只可能在这个范围里。不框住的话会误报场景里的红色物体
        （实测：木质地板被当成红品，还把截图拉成一条废图）。
        """
        if not self._anchor:
            return None
        nx, ny, nw, nh = self._anchor
        c = self.cfg
        px = int(nh * c.panel_pad_x_ratio)
        py_top = int(nh * c.panel_pad_top_ratio)
        py_bot = int(nh * c.panel_pad_bot_ratio)
        return (max(nx - px, 0), max(ny - py_top, 0),
                nw + px * 2, nh + py_top + py_bot)

    def _label_exclude(self, pad_x: int | None = None,
                       pad_y: int | None = None) -> tuple[int, int, int, int] | None:
        """
        容器名字标签的矩形（比 OCR 给的字框往外扩一圈）。

        标签的**底色本身也是红的**，和红品同色系，所以要在色块检测时排除掉。
        必须比字框大：OCR 给的是文字范围，红色底比文字宽出去不少
        （实测字框 105x21，红底约 226x52）。
        """
        if not self._anchor:
            return None
        nx, ny, nw, nh = self._anchor
        # 实测：字框 110x23，红底约 226x52 —— 大约各方向外扩 0.6x 字宽 / 0.8x 字高。
        # 别给太大：给大了会盖到紧挨着标签下面的第一个格子，把真红品误伤掉
        # （踩过一次：底部多出 9px，红品被判「和标签重叠 7%」直接丢掉）。
        px = pad_x if pad_x is not None else int(nw * 0.6)
        py = pad_y if pad_y is not None else int(nh * 0.8)
        return (max(nx - px, 0), max(ny - py, 0), nw + px * 2, nh + py * 2)

    def _ocr(self, work: np.ndarray) -> list[dict] | None:
        """
        跑一次 OCR。返回 None 表示本轮被限频跳过（区别于 [] = 跑了但没命中）。

        有 anchor 就只扫名字那一小块，能用更短的间隔；
        没有就扫整个工作区（贵，300ms），间隔也长一些。
        """
        now = time.perf_counter()
        if self._work_shape != work.shape[:2]:
            self._idle_anchor = None
            self._work_shape = work.shape[:2]
        region = self.anchor_region()
        cached_idle = not self.in_session and self._idle_anchor is not None
        if cached_idle and now - self._last_full_ocr_t >= self.cfg.ocr_full_interval:
            region = None
        if region is None and self.cfg.ocr_region:
            from roi import to_pixels
            region = to_pixels(tuple(self.cfg.ocr_region), work.shape)

        iv = (min(self.cfg.ocr_interval, 0.2) if cached_idle
              else self.cfg.ocr_interval if region is None else self.cfg.ocr_interval_fast)
        if iv > 0 and (now - self._last_ocr_t) < iv:
            self.ocr_skipped += 1
            return None
        self._last_ocr_t = now
        if region is None:
            self._last_full_ocr_t = now

        if region:
            rx, ry, rw, rh = region
            rx = min(rx, max(work.shape[1] - 1, 0))
            ry = min(ry, max(work.shape[0] - 1, 0))
            sub = work[ry:ry + rh, rx:rx + rw]
            if sub.size == 0:
                return []
        else:
            rx = ry = 0
            sub = work

        # 跑多个放大倍数取并集。两边盲区互补 —— 单跑任一遍都会漏掉一类容器名，
        # 而漏掉名字整条链路就不开会话（实测踩过，这是"出红没检测到"的根因）。
        #
        # 惰性：第一个倍数有命中就停。理由：
        #   * find_keyword 只返回**含关键词**的行，所以第一个命中一定可用，
        #     不存在"1.0 只读出一半、需要 1.5 补全"的情况。
        #   * 实测 1.0 和 1.5 对「保险箱」都能认出来，命中后再跑 1.5 是白花的
        #     —— 而 1.5 的 OCR 成本是 1.0 的两倍（103ms vs 51ms）。
        #   * 1.0 没命中时仍然会跑 1.5，所以"只有 1.5 认得出"的那类名字照旧能抓到。
        t0 = time.perf_counter()
        scales = getattr(self.cfg, "ocr_upscales", (self.cfg.ocr_upscale,))
        hits: list[dict] = []
        seen: set[str] = set()
        scales_run = 0
        for sc in scales:
            im = sub if sc == 1.0 else cv2.resize(
                sub, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
            scales_run += 1
            for h in ocr.find_keyword(im, self.cfg.container_keyword,
                                      self.cfg.ocr_lang, 1.0):
                key = h["flat"]
                if len(key) > self.cfg.container_name_max_len:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                if sc != 1.0:      # 坐标还原回原尺度
                    bx, by, bw, bh = h["bbox"]
                    h = {**h, "bbox": (bx / sc, by / sc, bw / sc, bh / sc)}
                hits.append(h)
            if hits:
                break
        self.ocr_ms_total += (time.perf_counter() - t0) * 1000
        self.ocr_calls += 1
        self.ocr_scales_run += scales_run

        if rx or ry:
            out = []
            for h in hits:
                bx, by, bw, bh = h["bbox"]
                out.append({**h, "bbox": (bx + rx, by + ry, bw, bh)})
            hits = out
        return hits

    # -- 会话 --------------------------------------------------------------
    def _end_session(self, reason: str) -> dict:
        s = self.session
        assert s is not None
        s["end"] = datetime.now().isoformat(timespec="seconds")
        s["end_reason"] = reason
        self.total += 1
        if s["red"]:
            self.streak = 0
            event = "container_red"
        else:
            self.streak += 1
            event = "container_clean"
        s["streak_after"] = self.streak
        self.history.append(s)
        self.in_session = False
        self.session = None
        self.misses = 0
        self._red_hist = []
        self._gray_streak = 0
        self._last_ocr_t = 0.0        # 立刻允许下一次 OCR，别白等限频周期
        # 清掉锚点和模板：IDLE 阶段必须用**全区域** OCR。
        # 小区域 OCR 误读率高（实测「机密保险箱」被读成过「也保險粕」
        # 「密保险箱」「机密保殓霜」），留着锚点会让下一个容器检测不到。
        self._anchor = None
        self._tpl = None
        return {"event": event, "state": "IDLE", "session": s, "streak": self.streak}

    def abort_session(self, reason: str) -> dict:
        """Stopped/occluded observations cannot establish a clean container."""
        s = self.session
        assert s is not None
        s['end'] = datetime.now().isoformat(timespec='seconds')
        s['end_reason'] = reason
        self.in_session = False
        self.session = None
        self._anchor = self._tpl = None
        self._red_hist = []
        self.misses = self._gray_streak = 0
        self._last_ocr_t = 0.0
        return {'event': 'session_incomplete', 'state': 'IDLE', 'session': s,
                'streak': self.streak, 'panel': None, 'red_now': []}

    def step(self, frame: np.ndarray) -> dict:
        """
        跑一轮，返回本轮事件。

        {"event": None | "session_start" | "container_red" | "container_clean",
         "state": "IDLE" | "SESSION", "session": dict | None,
         "streak": int, "panel": ndarray | None, "note": str | None}
        """
        work = self._work(frame)
        blank = {"event": None, "state": "IDLE" if not self.in_session else "SESSION",
                 "session": self.session, "streak": self.streak,
                 "panel": None, "note": None, "red_now": None}

        # ---------------- IDLE ----------------
        if not self.in_session:
            hits = self._ocr(work)
            if hits is None:
                return {**blank, "note": "OCR 限频跳过"}
            if not hits:
                return blank

            # 太长的不像容器名（见 Config.container_name_max_len 的说明）
            hits = [h for h in hits
                    if len(h["flat"]) <= self.cfg.container_name_max_len]
            if not hits:
                return {**blank,
                        "note": f"命中的文字过长，不像容器名（>{self.cfg.container_name_max_len} 字）"}
            nx, ny, nw, nh = (int(round(v)) for v in hits[0]["bbox"])
            self._anchor = (nx, ny, nw, nh)
            self._idle_anchor = self._anchor
            self._last_ocr_t = 0.0        # 下一轮就能用小区域再确认
            self.in_session = True
            self.misses = 0
            self._red_hist = []
            self._gray_streak = 0
            self.session = {
                "start": datetime.now().isoformat(timespec="seconds"),
                "ocr_text": hits[0]["flat"],
                "name_bbox": (nx, ny, nw, nh),
                "red": False,
                "red_blobs": [],
                "observations": 0,
            }
            # 截下名字标签的模板，会话内靠它判面板存活（见 panel_alive_score）
            self._build_tpl(work)
            # 记下面板范围：红品一定在里面，存图时也只裁这一块
            pr = self.panel_rect()
            if pr:
                self.session["panel_rect"] = [int(v) for v in pr]
            # 开箱这一轮也要跑色块检测 —— 工具启动时面板可能已经开着、红品已经
            # 躺在里面了。只在后续 SESSION 轮里检会漏掉这种「打开即已就绪」的情况。
            red_now = self._scan_red(work)
            # panel 要返回工作区图，否则事件没有截图可存，前端就是个黑块
            return {"event": "session_start", "state": "SESSION",
                    "session": self.session, "streak": self.streak,
                    "panel": work, "note": None, "red_now": red_now}

        # ---------------- SESSION ----------------
        self.session["observations"] += 1

        # 面板存活判定用**模板匹配**，不用 OCR。
        # 小区域 OCR 误读率太高（同一个「机密保险箱」被读成过
        # 「密保险箱」「也保險粕」「机密保殓霜」），拿它判消失会把一个容器
        # 拆成好几条记录 —— 实测就是这么坏的。标签是静态 UI，模板匹配
        # 同一帧 NCC=1.0、空帧 0.0，又快又准。
        score = panel_alive_score(work, self._tpl, self._tpl_pos,
                                  self.cfg.tpl_search_pad)
        self.last_score = score
        self.session["last_score"] = round(score, 3)

        if score >= self.cfg.tpl_match_min:
            self.misses = 0
            self._gray_streak = 0
        elif score < self.cfg.tpl_gone_max:
            # 明确低于下限，算一次消失
            self.misses += 1
            self._gray_streak = 0
            if self.misses >= self.miss_need:
                r = self._end_session(f"面板消失 (NCC={score:.2f})")
                r["panel"] = None
                return r
        else:
            # 灰区：不立刻算消失（容忍抖动），但**不能无限待** ——
            # 实测面板已消失、分数停在灰区，会话被永久卡住、永远不结算。
            self._gray_streak += 1
            self.session["gray"] = self._gray_streak
            if self._gray_streak >= self.cfg.tpl_gray_max_rounds:
                self.misses += 1
                if self.misses >= self.miss_need:
                    r = self._end_session(
                        f"灰区超时 (NCC={score:.2f} 连续 {self._gray_streak} 轮)")
                    r["panel"] = None
                    return r

        if score < self.cfg.tpl_match_min:
            # Closing debounce preserves the session, but these frames cannot
            # provide evidence about items in a panel that may already be gone.
            self._red_hist.append([])
            self._red_hist = self._red_hist[-self.cfg.red_confirm_window:]
            return {**blank, "note": "面板未确认，暂停红品判定"}
        red_now = self._scan_red(work)
        return {"event": None, "state": "SESSION", "session": self.session,
                "streak": self.streak, "panel": work, "note": None,
                "red_now": red_now}

    def _filter_by_text(self, panel: np.ndarray,
                        blobs: list[dict]) -> list[dict]:
        """对每个红块 OCR 它自己那一小块，用物品名做最后判定。"""
        if not blobs:
            return blobs
        cfg = self.cfg
        kept: list[dict] = []
        for b in blobs:
            bx, by, bw, bh = b["bbox"]
            # 留白要给足：物品名在槽位左上角，红块的 bbox 未必盖住它。
            # 实测 pad=15 读不出任何字，pad=45 才读出「航天实验」。
            pad = int(max(20, min(bw, bh) * 0.45))
            x1, y1 = max(bx - pad, 0), max(by - pad, 0)
            x2, y2 = min(bx + bw + pad, panel.shape[1]), min(by + bh + pad, panel.shape[0])
            crop = panel[y1:y2, x1:x2]
            text = ""
            first_line = ""
            if crop.size:
                # 先查记忆：键是裁剪内容的哈希。搜刮时同一个红块会在连续多轮里
                # 反复出现（红品结果要 latch 好几轮），而面板画面大部分时候是
                # 静止的 —— 命中缓存就能整段跳过 OCR（实测这段占该轮 80%）。
                ckey = hashlib.blake2b(crop.tobytes(), digest_size=8).digest()
                cached = self._text_cache.get(ckey)
                if cached is not None:
                    text, first_line = cached
                    self.text_cache_hits += 1
                else:
                    item_t0 = time.perf_counter()
                    for sc in getattr(cfg, "ocr_upscales", (1.0,)):
                        im = crop if sc == 1.0 else cv2.resize(
                            crop, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
                        for ln in ocr.recognize(im, lang_tag=cfg.ocr_lang):
                            flat = ocr.flatten(ln["text"])
                            text += flat
                            if not first_line and flat:
                                # 物品名在槽位左上角、价格在下面，第一行就是名字
                                first_line = flat
                        self.item_ocr_calls += 1
                    self.item_ocr_ms_total += (time.perf_counter() - item_t0) * 1000
                    # 简单的容量上限：超了就丢最早进来的那个
                    if len(self._text_cache) >= 64:
                        self._text_cache.pop(next(iter(self._text_cache)))
                    self._text_cache[ckey] = (text, first_line)
            b["ocr_text"] = text
            # 只给界面看的短名字（ocr_text 拼了多行，含价格，做匹配用不上显示）
            b["ocr_name"] = first_line
            bad = [w for w in cfg.red_exclude_words if w in text]
            if bad:
                b["reject"] = list(b.get("reject", [])) + ["排除词:" + "/".join(bad)]
                b["accepted"] = False
                continue
            if cfg.red_require_text and not text:
                b["reject"] = list(b.get("reject", [])) + ["无文字(疑似场景)"]
                b["accepted"] = False
                continue
            kept.append(b)
        return kept

    def _build_tpl(self, work: np.ndarray) -> None:
        """
        截下名字标签作为模板。

        要连标签的**红底**一起截，不能只截文字 —— 实测字框 110x23，
        而红底约 226x52，只截文字的话周围的红底会污染相关性。
        """
        if not self._anchor:
            self._tpl = None
            return
        nx, ny, nw, nh = self._anchor
        px, py = int(nw * 0.55), int(nh * 0.5)
        x0, y0 = max(nx - px, 0), max(ny - py, 0)
        x1 = min(nx + nw + px, work.shape[1])
        y1 = min(ny + nh + py, work.shape[0])
        t = work[y0:y1, x0:x1]
        if t.size and t.shape[0] >= 8 and t.shape[1] >= 8:
            self._tpl = t.copy()
            self._tpl_pos = (x0, y0)
        else:
            self._tpl = None

    def _scan_red(self, work: np.ndarray) -> list[dict]:
        """
        在工作区里找红品，命中就 latch 到当前会话。

        不依赖面板边界 —— 容器面板的尺寸和位置随容器类型变，但红品槽位底色
        是固定色相，在整块工作区里找就够了。
        容器名字标签底色也是红的（实测 #D29285 色相 5、#9C4B3F 色相 4），
        和红品同色系，靠颜色分不开 —— 用 OCR 给的名字 bbox 做位置排除。
        """
        rect = self.panel_rect()
        if rect is None:
            return []
        x, y, w, h = rect
        sub = work[y:y + h, x:x + w]
        if sub.size == 0:
            return []
        # 只在面板范围内找；再把坐标加回偏移，下游仍用工作区坐标
        ex = self._label_exclude()
        ex_local = ((ex[0] - x, ex[1] - y, ex[2], ex[3]) if ex else None)
        # name_h 用来推格子尺寸（大红至少两格）
        name_h = self._anchor[3] if self._anchor else None
        blobs = find_red_blobs(sub, self.cfg, exclude=ex_local, name_h=name_h)
        accepted = [b for b in blobs["blobs"] if b["accepted"]]
        # Establish temporal geometry before expensive item OCR. OCR on the first
        # brief highlight can block capture of its second frame entirely.
        self._red_hist.append([(b['bbox'][0] + x, b['bbox'][1] + y,
                                b['bbox'][2], b['bbox'][3]) for b in accepted])
        self._red_hist = self._red_hist[-self.cfg.red_confirm_window:]
        eligible = []
        for b in accepted:
            bx, by, bw, bh = b['bbox']
            votes = sum(any(_bbox_iou((bx+x, by+y, bw, bh), old) >= 0.3
                            for old in frame) for frame in self._red_hist)
            b['confirm_votes'] = votes
            if votes >= self.cfg.red_confirm_rounds:
                eligible.append(b)
        # Text exclusions still run before anything is latched as a red item.
        accepted = self._filter_by_text(sub, eligible)
        for b in blobs["blobs"]:
            bx, by, bw, bh = b["bbox"]
            b["bbox"] = (bx + x, by + y, bw, bh)
        self.last_candidates = blobs["blobs"][:8]
        # 最近 win 轮里有 rounds 轮命中才 latch
        # A vote must refer to the same location, not just any red pixel in
        # the panel. Count at most one vote per frame for each current item.
        confirmed_blobs = []
        for b in accepted:
            votes = b['confirm_votes']
            if votes >= self.cfg.red_confirm_rounds:
                confirmed_blobs.append(b)
        confirmed = bool(confirmed_blobs)
        if confirmed:
            self.session["red"] = True          # latch：一旦确认就记住
            # 只在**本轮真的看到色块**时更新 red_blobs。
            # 滑动窗口里可能出现"本轮空、但前两轮命中"-> confirmed 为真而
            # accepted 为空，直接赋值会把之前看到的色块覆盖掉（实测出红事件
            # red_count 变成 0，截图里也没框）。
            self.session["red_blobs"] = confirmed_blobs
        return confirmed_blobs

    # -- 汇总 --------------------------------------------------------------
    def summary(self) -> str:
        avg = self.ocr_ms_total / self.ocr_calls if self.ocr_calls else 0.0
        ran = self.ocr_calls + self.ocr_skipped
        save = self.ocr_skipped / ran * 100 if ran else 0.0
        return (f"共结算 {self.total} 个容器，当前连续未出红 {self.streak} 个；"
                f"OCR 实跑 {self.ocr_calls} 次 / 限频跳过 {self.ocr_skipped} 次"
                f"（省了 {save:.0f}%），平均 {avg:.0f}ms；"
                f"放大倍数跑了 {self.ocr_scales_run} 遍，"
                f"逐块物品名缓存命中 {self.text_cache_hits} 次")
