"""
后台运行器：在独立线程里跑 track 链路，记录每次检测的事件和时间，并存截图。

给 Web UI 用。也可以单独 headless 跑（`python main.py serve` 是带 UI 的版本）。

线程注意：mss 的实例不能跨线程共用，所以 Grabber 在**工作线程内部**创建和销毁。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace, asdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from capture import Grabber, window_visible_ratio
from detect import Config
from imgio import imwrite_u
from track import Tracker
from storage import RunStore

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"


def _crop_around(work: np.ndarray, session: dict,
                 blobs: list[dict] | None = None) -> tuple[np.ndarray, tuple[int, int]]:
    """
    裁一块出来存图，返回 (裁剪图, (ox, oy))。

    直接存整个工作区的话一张就是 1024x1440 的 PNG（~1MB），跑一局下来很大。

    **只裁 OCR 名字推出来的面板范围**，不按红品 bbox 做并集。
    踩过：原来把红品 bbox 也并进来，结果场景里的红色物体（木质地板）
    在画面最底部，一下把截图拉成 282x1327 的废条，什么都看不出来。
    红品现在只会在面板范围内被检出，所以裁这一块就够。
    """
    rect = session.get("panel_rect")
    if rect:
        x0, y0, w, h = (int(v) for v in rect)
    else:
        nb = session.get("name_bbox")
        if not nb:
            return work, (0, 0)
        nx, ny, nw, nh = nb
        px, py_top, py_bot = int(nh * 6), int(nh * 2), int(nh * 15)
        x0, y0 = max(nx - px, 0), max(ny - py_top, 0)
        w, h = nw + px * 2, nh + py_top + py_bot

    x1 = min(x0 + w, work.shape[1])
    y1 = min(y0 + h, work.shape[0])
    x0 = min(x0, max(x1 - 1, 0))
    y0 = min(y0, max(y1 - 1, 0))
    crop = work[y0:y1, x0:x1]
    return (crop, (x0, y0)) if crop.size else (work, (0, 0))


def _annotate(panel: np.ndarray, blobs: list[dict], origin=(0, 0)) -> np.ndarray:
    """在截图上把命中的红品框出来。origin 是这块图在工作区里的左上角偏移。"""
    vis = panel.copy()
    ox, oy = origin
    for b in blobs:
        x, y, w, h = b["bbox"]
        x, y = x - ox, y - oy
        if x + w < 0 or y + h < 0 or x > vis.shape[1] or y > vis.shape[0]:
            continue
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 255), 2)
        # hue 模式下没有 core_match，别硬显示成 "core=None"
        tag = (f"core={b['core_match']}" if b.get("core_match") is not None
               else f"{w}x{h}")
        cv2.putText(vis, tag, (max(x, 2), max(y - 5, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
    return vis


class Runner:
    """抓屏 -> track -> 记录事件 + 截图。线程安全地暴露状态给 Web UI。"""

    def __init__(self, cfg: Config, window: str = "", monitor_only: bool = False,
                 monitor_index: int = 1, interval: float = 0.4, miss_need: int = 3):
        self.cfg = cfg
        self.window = window
        self.monitor_only = monitor_only
        self.monitor_index = monitor_index
        self.interval = interval
        self.miss_need = miss_need

        self._lock = threading.Lock()
        self._control_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._events: list[dict] = []
        self._seq = 0
        self._status: dict = {
            "running": False, "state": "IDLE", "streak": 0, "total": 0,
            "current": None, "target": None, "error": None,
            "misses": 0, "miss_need": miss_need,
            "started_at": None, "stopped_at": None,
            "obs": 0, "ocr_ms": 0.0, "ocr_calls": 0, "foreground": True,
            "misses": 0, "miss_need": miss_need,
        }
        self.run_dir: Path | None = None
        self._store: RunStore | None = None
        self.environment = None

    @property
    def store(self) -> RunStore:
        with self._control_lock:
            if self._store is None:
                self._store = RunStore(self.run_dir.parent if self.run_dir else RUNS)
            return self._store

    # -- 状态查询 ----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {**self._status, "run_dir": self.run_dir.name if self.run_dir else None}

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    # -- 控制 --------------------------------------------------------------
    def start(self) -> bool:
        with self._control_lock:
            return self._start()

    def _start(self) -> bool:
        if self.environment and self.environment.status()['status'] != 'ready':
            with self._lock:
                self._status['error'] = self.environment.status()['message']
            return False
        if self._thread and self._thread.is_alive():
            return False
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.run_dir = RUNS / stamp
        self.run_dir.mkdir(parents=True, exist_ok=False)
        with self._lock:
            self._events = []
            self._seq = 0
            self._status.update({
                "running": True, "state": "IDLE", "streak": 0, "total": 0,
                "current": None, "error": None, "obs": 0,
                "ocr_ms": 0.0, "ocr_calls": 0,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "stopped_at": None,
            })
        self._stop.clear()
        try:
            self.store.update_run(stamp, {
                'started_at': self._status['started_at'], 'stopped_at': None,
                'status': 'running', 'window': self.window, 'config': asdict(self.cfg),
                'interval': self.interval, 'miss_need': self.miss_need,
            })
        except Exception as e:
            with self._lock:
                self._status.update({'running': False, 'error': f'创建运行记录失败: {e}'})
            return False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._thread and self._thread.is_alive():
            with self._lock:
                self._status['error'] = '停止请求已发送，等待当前识别完成'

    def _crop_rect(self) -> tuple | None:
        """要抓的工作区，归一化 (fx,fy,fw,fh)。返回 None 表示抓整窗。"""
        if self.cfg.work_region:
            return tuple(self.cfg.work_region)
        rf = max(min(self.cfg.right_frac, 1.0), 0.0)
        if rf >= 1.0:
            return None
        return (round(1.0 - rf, 4), 0.0, round(rf, 4), 1.0)

    # -- 工作线程 ----------------------------------------------------------
    def _loop(self) -> None:
        try:
            self._run_loop()
        except Exception as e:
            # Cover setup failures as well as the main observation loop.
            with self._lock:
                self._status.update({'running': False, 'state': 'IDLE', 'current': None,
                    'error': f'{type(e).__name__}: {e}',
                    'stopped_at': datetime.now().isoformat(timespec='seconds')})
            try:
                self.store.update_run(self.run_dir.name, {
                    'status': 'error', 'error': self.status()['error'],
                    'stopped_at': self.status()['stopped_at']})
            except Exception:
                pass  # The original error remains visible, JSONL stays on disk.

    def _run_loop(self) -> None:
        # Grabber 必须在本线程内创建（mss 实例不能跨线程）
        try:
            # 只抓工作区那块，不抓整个窗口 —— 实测 29ms -> 14ms（2.1 倍）。
            # 工作区是配置里就已知的（手画的 work_region 或默认的右侧 right_frac），
            # 不需要先抓全屏再裁。
            g = Grabber(window_substr=self.window,
                        monitor=self.monitor_index,
                        force_monitor=self.monitor_only,
                        allow_fallback=self.monitor_only,
                        crop=self._crop_rect())
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._status.update({"running": False, "error": f"抓屏初始化失败: {e}"})
            self.store.update_run(self.run_dir.name, {
                'status': 'error', 'error': str(e),
                'stopped_at': datetime.now().isoformat(timespec='seconds')})
            return

        with self._lock:
            self._status["target"] = g.target_name
        self.store.update_run(self.run_dir.name, {'target': g.target_name})

        # 抓到的是工作区，所以告诉 Tracker「帧本身就是工作区」
        tr = Tracker(replace(self.cfg, frame_is_work=bool(self._crop_rect())),
                     miss_need=self.miss_need)
        # **只在会话健康时**记录画面。
        # 踩过：会话结束前那几轮模板匹配已经失败（面板早就不在了），
        # 但 SESSION 状态照样返回工作区图，于是 last_panel 被那些
        # 「已经没有面板」的帧覆盖 —— 存下来的「无红」截图是张天花板。
        last_panel, last_blobs = None, []
        # 红品第一次出现那一刻的画面。**不能用会话结束时的帧** ——
        # 实测踩过：出红事件的截图存的是结束前那一帧，那时候画面早就飘走了，
        # 存下来是一张无关的场景图，黄框框着空白，完全没法看。
        # 用视频当输入时尤其明显（内容每帧都在变）。
        red_snapshot = None

        # 游戏不在前台时喂的空帧。空白 -> OCR 无命中、网格检测失败，
        # 状态机会自然走 miss 并在几轮内结束掉未完成的会话。
        blank = np.zeros((64, 64, 3), np.uint8)

        # 逐轮状态轨迹。出问题时这个文件能直接看出「会话为什么没结束」
        # —— 是名字一直没消失，还是轮询太慢没抓到消失的那几帧。
        try:
            trace = (self.run_dir / "trace.jsonl").open("w", encoding="utf-8")
        except OSError:
            g.close()
            raise
        t_start = time.perf_counter()
        last_flush = last_meta = t_start
        elapsed_total = 0.0
        fatal_error = None

        # 单轮异常不该让整局采集挂掉 —— 用户是把它挂一晚上跑的。
        # 但连续出错说明是系统性问题（窗口没了、权限变了），那就停。
        consec_err = 0
        MAX_CONSEC_ERR = 10

        try:
            while not self._stop.is_set():
                cycle_start = time.perf_counter()
                try:
                    # 抓屏抓的是桌面合成结果，别的窗口盖住游戏时拿到的就是那个窗口。
                    # 所以只在前台时分析，否则喂空帧。
                    # 判断"有没有被别的窗口盖住"，而不是"在不在前台" ——
                    # 多屏下游戏放一块屏、浏览器放另一块屏时它从来不是前台，
                    # 但完全可见、抓屏正常。用前台判据会让工具直接瘫痪。
                    fg = True
                    if g.window is not None:
                        fg = window_visible_ratio(g.window["hwnd"], g.region()) >= 0.6
                    frame = g.grab() if fg else blank
                    captured_at = time.perf_counter()
                    res = (tr.abort_session('窗口被遮挡，观察中断')
                           if not fg and tr.in_session else tr.step(frame))
                    analyzed_at = time.perf_counter()
                except Exception as e:  # noqa: BLE001
                    consec_err += 1
                    with self._lock:
                        self._status["error"] = (
                            f"第 {consec_err} 次连续异常: {type(e).__name__}: {e}")
                    if consec_err >= MAX_CONSEC_ERR:
                        fatal_error = self._status['error']
                        break
                    self._stop.wait(self.interval)
                    continue
                consec_err = 0
                with self._lock:
                    self._status["error"] = None

                if res.get("panel") is not None:
                    sess = res.get("session") or {}
                    # 只认**模板分数确认还活着**的帧。
                    # 不能用 misses==0 判断：灰区轮次的 misses 也是 0（超时前不计），
                    # 而那时面板可能已经消失了 —— 实测会把一张场景（洗手池、窗户）
                    # 当成面板存下来。
                    # session_start 那轮 last_score 还没测过（模板刚建），单独放过
                    if (res["event"] == "session_start"
                            or tr.last_score >= tr.cfg.tpl_match_min):
                        last_panel = res["panel"]
                        last_blobs = sess.get("red_blobs", [])
                        # 本轮看到红品 -> 把这一帧留下来当截图素材。
                        # **色块列表要用 session 里 latch 住的那份**，不能用本轮
                        # 的 red_now —— 确认靠滑动窗口，本轮可能恰好没命中，
                        # 那 red_now 就是空的，存下来 red_count=0、截图也没框。
                        # 实测踩过：事件判成"出红"但红品数是 0。
                        if res.get("red_now"):
                            latched = sess.get("red_blobs") or res["red_now"]
                            red_snapshot = (res["panel"].copy(), latched)

                # 出红事件用「红品出现那一刻」的画面，其余用当前帧
                if res["event"] == "container_red" and red_snapshot is not None:
                    use_panel, use_blobs = red_snapshot
                else:
                    use_panel, use_blobs = last_panel, last_blobs

                try:
                    self._record(res, tr, use_panel, use_blobs)
                except Exception as e:  # noqa: BLE001
                    # Stop visibly if durable recording fails; otherwise the
                    # displayed totals would diverge from the saved history.
                    fatal_error = f"记录事件失败: {e}"
                    with self._lock:
                        self._status["error"] = fatal_error
                    break

                with self._lock:
                    self._status.update({
                        "state": res["state"],
                        "streak": tr.streak,
                        "total": tr.total,
                        # 用 .get 而不是 [] —— 会话字段变过一次（网格检测拿掉后
                        # cells 没了），直接下标会让整个采集线程在第一个容器
                        # 识别出来时崩掉 0x0 错误。
                        "current": ({"ocr_text": res["session"].get("ocr_text", ""),
                                     "name_bbox": list(res["session"].get("name_bbox") or []),
                                     "red": res["session"].get("red", False),
                                     "observations": res["session"].get("observations", 0)}
                                    if res.get("session") else None),
                        "obs": self._status["obs"] + 1,
                        "misses": tr.misses,
                        "miss_need": tr.miss_need,
                        "ocr_calls": tr.ocr_calls,
                        "ocr_ms": round(tr.ocr_ms_total / tr.ocr_calls, 1) if tr.ocr_calls else 0.0,
                        "foreground": fg,
                        "item_ocr_calls": tr.item_ocr_calls,
                        "item_ocr_ms": round(tr.item_ocr_ms_total / tr.item_ocr_calls, 1)
                            if tr.item_ocr_calls else 0.0,
                        "text_cache_hits": tr.text_cache_hits,
                    })

                recorded_at = time.perf_counter()
                cycle_ms = (recorded_at - cycle_start) * 1000
                elapsed_total += cycle_ms
                timings = {'capture_ms': round((captured_at - cycle_start) * 1000, 2),
                           'detect_ms': round((analyzed_at - captured_at) * 1000, 2),
                           'record_ms': round((recorded_at - analyzed_at) * 1000, 2),
                           'cycle_ms': round(cycle_ms, 2)}
                with self._lock:
                    self._status.update(timings)
                    self._status['cycle_avg_ms'] = round(elapsed_total / self._status['obs'], 2)

                # 逐轮轨迹：状态 / 丢失计数 / 本轮事件 / OCR 次数。
                # 会话「该结束却没结束」时，看这个就知道是名字一直没消失，
                # 还是轮询太慢漏掉了消失的那几帧。
                trace.write(json.dumps({
                    "t": round(time.perf_counter() - t_start, 2),
                    "state": res["state"],
                    "event": res["event"],
                    "misses": tr.misses,
                    "observations": (res.get("session") or {}).get("observations"),
                    "red": (res.get("session") or {}).get("red"),
                    "ocr_calls": tr.ocr_calls,
                    "ocr_skipped": tr.ocr_skipped,
                    "score": round(tr.last_score, 3),
                    "note": res.get("note"),
                    **timings,
                    "foreground": fg,
                    "item_ocr_calls": tr.item_ocr_calls,
                    "text_cache_hits": tr.text_cache_hits,
                    "candidates": (tr.last_candidates if fg and tr.in_session
                                   and (res['event'] == 'session_start'
                                        or tr.last_score >= tr.cfg.tpl_match_min) else []),
                }, ensure_ascii=False) + "\n")

                now = time.perf_counter()
                if res['event'] or now - last_flush >= 2:
                    trace.flush()
                    last_flush = now
                if now - last_meta >= 5:
                    self.store.update_run(self.run_dir.name, {'metrics': self.status()})
                    last_meta = now

                if res["event"] in ("container_red", "container_clean", "session_incomplete"):
                    red_snapshot = None      # 会话结算，快照用完了
                    last_panel, last_blobs = None, []

                self._stop.wait(min(self.interval, self.cfg.session_interval)
                                if tr.in_session else self.interval)
        except Exception as e:
            fatal_error = f'{type(e).__name__}: {e}'
            with self._lock:
                self._status['error'] = fatal_error
        finally:
            if tr.in_session:
                try:
                    aborted = tr.abort_session('检测异常中断' if fatal_error else '用户停止检测')
                    self._record(aborted, tr, last_panel, last_blobs)
                except Exception as e:
                    fatal_error = f'保存未完成会话失败: {e}'
            try:
                trace.close()
            except OSError:
                pass
            g.close()
            with self._lock:
                self._status.update({"running": False, "state": "IDLE", "current": None,
                                     'stopped_at': datetime.now().isoformat(timespec='seconds')})
            self.store.update_run(self.run_dir.name, {
                'status': 'error' if fatal_error else 'stopped', 'error': fatal_error,
                'stopped_at': self.status()['stopped_at'],
                'duration_s': round(time.perf_counter() - t_start, 2),
                'metrics': self.status(),
            })

    # -- 事件记录 ----------------------------------------------------------
    def _record(self, res: dict, tr: Tracker, panel, blobs) -> None:
        ev = res["event"]
        if ev not in ("session_start", "container_red", "container_clean", "session_incomplete"):
            return

        self._seq += 1
        seq = self._seq
        sess = res.get("session") or {}
        shots: list[str] = []

        if panel is not None and panel.size:
            # panel 是整个工作区，裁到「名字 + 命中红品」的并集再存
            # （否则一张就是 1MB 级，而且红品可能离名字很远被切掉）
            crop, origin = _crop_around(panel, sess, blobs)
            name = f"{seq:04d}_panel.png"
            imwrite_u(self.run_dir / name, crop)
            shots.append(name)
            if ev == "container_red" and blobs:
                aname = f"{seq:04d}_red_annotated.png"
                imwrite_u(self.run_dir / aname, _annotate(crop, blobs, origin))
                shots.append(aname)

        kind = {"session_start": "start",
                "container_red": "red",
                "container_clean": "clean", "session_incomplete": "incomplete"}[ev]
        rec = {
            "seq": seq,
            "t": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "text": sess.get("ocr_text", ""),
            "name_bbox": list(sess.get("name_bbox") or []),
            "observations": sess.get("observations"),
            "red_count": len(blobs) if ev == "container_red" else 0,
            # 大金的**物品名**（从红块那一小块 OCR 出来的）。
            # 既给用户看"出了什么"，也是排除词判据的依据。
            "item_names": [b.get("ocr_name") or b.get("ocr_text", "")
                           for b in (blobs or []) if b.get("ocr_text")],
            "streak": res.get("streak", tr.streak),
            "shots": shots,
            'start': sess.get('start'), 'end': sess.get('end'),
            'end_reason': sess.get('end_reason'),
            'panel_rect': sess.get('panel_rect'),
            'red_observed': sess.get('red', False),
            'blobs': blobs if ev == 'container_red' else [],
        }
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.store.append_event(self.run_dir.name, rec)
        with self._lock:
            self._events.append(rec)
