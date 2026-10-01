"""
本地 Web UI：浏览器里看实时状态、事件时间线、每次检测的截图。

用标准库的 ThreadingHTTPServer，不引额外依赖。
后端还是 Python（要抓屏和跑 OCR），浏览器只做展示。

**重要限制**：截图走的是桌面合成，如果浏览器窗口盖住游戏，抓到的就是浏览器。
要么把浏览器放第二块屏 / 手机（绑 0.0.0.0），要么让游戏保持在最前。
"""

from __future__ import annotations

import json
import hashlib
import mimetypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from runner import RUNS, Runner
from storage import valid_run
from history_ui import PAGE as HISTORY_PAGE

def server_info(runner: Runner) -> dict:
    identity = str(runner.store.root.resolve()).casefold().encode('utf-8')
    return {'version': '0.2.1', 'storage_id': hashlib.sha256(identity).hexdigest()}


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# --------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>暗区 · 出红记录</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
    --fg:#e6e9ef; --dim:#8b93a5; --red:#e5484d; --green:#3fb950;
    --amber:#e3b341; --blue:#4493f8;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:1100px;margin:0 auto;padding:20px}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
  h1{font-size:18px;margin:0;font-weight:600}
  .pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;
        background:var(--panel2);color:var(--dim);border:1px solid var(--line)}
  .pill.on{background:rgba(63,185,80,.15);color:var(--green);border-color:rgba(63,185,80,.4)}
  .pill.err{background:rgba(229,72,77,.15);color:var(--red);border-color:rgba(229,72,77,.4)}
  .spacer{flex:1}
  button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}
  button:hover{border-color:var(--dim)}
  button.primary{background:var(--green);color:#04170a;border-color:var(--green)}
  button.danger{background:var(--red);color:#1a0405;border-color:var(--red)}
  button:disabled{opacity:.4;cursor:not-allowed}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
         gap:12px;margin-bottom:20px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .stat .k{font-size:12px;color:var(--dim);margin-bottom:6px}
  .stat .v{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}
  .stat.red .v{color:var(--red)}
  .stat.green .v{color:var(--green)}
  .stat .sub{font-size:11px;color:var(--dim);margin-top:4px}

  h2{font-size:13px;color:var(--dim);font-weight:600;margin:24px 0 10px;
     text-transform:uppercase;letter-spacing:.06em}
  .feed{display:flex;flex-direction:column;gap:10px}
  .card{display:flex;gap:14px;background:var(--panel);border:1px solid var(--line);
        border-radius:12px;padding:12px;align-items:flex-start}
  .card.red{border-color:rgba(229,72,77,.5);background:rgba(229,72,77,.06)}
  .card .thumb{width:104px;height:78px;object-fit:cover;border-radius:8px;
               border:1px solid var(--line);cursor:zoom-in;background:#000;flex:none}
  .card .body{flex:1;min-width:0}
  .card .top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px}
  .badge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px}
  .badge.red{background:var(--red);color:#fff}
  .badge.clean{background:var(--panel2);color:var(--dim);border:1px solid var(--line)}
  .badge.start{background:rgba(68,147,248,.18);color:var(--blue);border:1px solid rgba(68,147,248,.35)}
  .card .t{font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums}
  .card .meta{font-size:12px;color:var(--dim);display:flex;gap:12px;flex-wrap:wrap}
  .card .meta b{color:var(--fg);font-weight:600}
  .empty{color:var(--dim);text-align:center;padding:48px 0;font-size:13px}

  #lb{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;
      align-items:center;justify-content:center;cursor:zoom-out;z-index:99;padding:24px}
  #lb img{max-width:100%;max-height:100%;border-radius:8px}
  .err{background:rgba(229,72,77,.12);border:1px solid rgba(229,72,77,.4);
       color:var(--red);padding:10px 14px;border-radius:10px;margin-bottom:14px;
       font-size:13px;display:none}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>暗区 · 出红记录</h1>
    <a href="/history" style="color:var(--blue)">运行历史</a>
    <span class="pill" id="statePill">未运行</span>
    <span class="pill" id="fgPill" style="display:none">游戏不在前台</span>
    <div class="spacer"></div>
    <select id="winSel" title="选择游戏窗口" style="background:var(--panel2);color:var(--fg);
            border:1px solid var(--line);border-radius:8px;padding:8px 10px;
            font-size:13px;max-width:240px"></select>
    <button id="btnWin" title="重新扫描窗口列表">刷新列表</button>
    <button id="btnPrev" title="抓一帧看看实际抓到什么">预览</button>
    <select id="runSel" title="查看历史记录" style="background:var(--panel2);color:var(--fg);
            border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px"></select>
    <button id="btnStart" class="primary">开始检测</button>
    <button id="btnStop" class="danger" disabled>停止</button>
  </header>

  <div class="err" id="errBox"></div>
  <div class="card" style="margin-bottom:14px;align-items:center">
    <span id="envText" style="flex:1">正在检测中文 OCR 环境…</span>
    <button id="btnEnv" style="display:none">重新检测并安装</button>
  </div>

  <!-- 抓屏预览：选错窗口时必须让人一眼看出来 -->
  <div class="card" id="prevCard" style="margin-bottom:18px">
    <div class="body">
      <div class="top">
        <span class="badge start">抓屏预览</span>
        <span style="font-size:12px;color:var(--dim)">
          这是工具实际看到的画面，黄框 = 工作区。切到游戏再点刷新，
          <b style="color:var(--fg)">确认看到的是游戏而不是别的窗口</b>。
        </span>
      </div>
      <div id="prevEmpty" style="color:var(--dim);font-size:13px;padding:14px 0">
        还没选窗口 —— 右上角选一个，然后点【预览】
      </div>
      <img id="prevImg" style="display:none;max-width:100%;border-radius:8px;
           border:1px solid var(--line);margin-top:8px">
    </div>
  </div>

  <div class="stats">
    <div class="stat green"><div class="k">当前连续未出红</div>
      <div class="v" id="sStreak">0</div>
      <div class="sub" id="sStreakSub">个保险箱</div></div>
    <div class="stat"><div class="k">已结算保险箱</div>
      <div class="v" id="sTotal">0</div>
      <div class="sub">本次运行</div></div>
    <div class="stat red"><div class="k">出红次数</div>
      <div class="v" id="sReds">0</div>
      <div class="sub">本次运行</div></div>
    <div class="stat"><div class="k">容器名 OCR 平均耗时</div>
      <div class="v" id="sOcr">—</div>
      <div class="sub" id="sObs">0 轮</div></div>
    <div class="stat"><div class="k">每轮平均处理耗时</div>
      <div class="v" id="sCycle">—</div>
      <div class="sub" id="sItemOcr">物品 OCR：—</div></div>
  </div>

  <div class="card" id="curCard" style="display:none">
    <div class="body">
      <div class="top"><span class="badge start">进行中</span>
        <span id="curText"></span></div>
      <div class="meta" id="curMeta"></div>
    </div>
  </div>

  <h2>检测记录</h2>
  <div class="feed" id="feed"><div class="empty">还没有记录</div></div>
</div>

<div id="lb"><img id="lbImg" alt=""></div>

<script>
// 有任何 JS 错误直接显示在页面上，避免「页面看着正常但什么都不动」
window.onerror = function(msg, src, line){
  const b = document.getElementById('errBox');
  if(b){ b.textContent = 'JS 错误: ' + msg + ' (第 ' + line + ' 行)'; b.style.display = 'block'; }
};

const $ = id => document.getElementById(id);
let sinceSeq = 0, curRun = null, events = [], runsLoaded = false, commandError = '';

function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function fmtCells(c){
  if(!c) return '—';
  const r = v => Math.round(v);
  return r(c[0]) + '×' + r(c[1]);
}

function card(e){
  const img = e.shots && e.shots.length
    ? `<img class="thumb" src="/shot/${encodeURIComponent(e.run||curRun)}/${encodeURIComponent(e.shots[0])}" onclick="zoom(this.src)">`
    : `<div class="thumb"></div>`;
  const badge = e.kind==='red' ? '<span class="badge red">出红</span>'
              : e.kind==='clean' ? '<span class="badge clean">无红</span>'
              : e.kind==='incomplete' ? '<span class="badge start">未完成</span>'
              : '<span class="badge start">开箱</span>';
  const shown = e.shots && e.shots.length > 1
    ? e.shots.slice(1).map(s =>
        `<a href="#" onclick="zoom('/shot/${encodeURIComponent(e.run||curRun)}/${encodeURIComponent(s)}');return false"
            style="color:var(--amber)">标注图</a>`).join('')
    : '';
  const streak = e.kind==='clean'
    ? `连续 <b>${e.streak}</b> 个未出红`
    : e.kind==='red' ? `连续 <b>${e.streak}</b> 个被打断` : '';
  return `<div class="card ${e.kind==='red'?'red':''}">
    ${img}
    <div class="body">
      <div class="top">${badge}<span class="t">${esc((e.t||'').replace('T',' '))}</span>
        ${e.text?`<span style="font-size:12px;color:var(--dim)">${esc(e.text)}</span>`:''}</div>
      <div class="meta">
        ${e.item_names && e.item_names.filter(Boolean).length
            ? `<span>物品 OCR <b style="color:var(--amber)">${esc(e.item_names.filter(Boolean).join(' / '))}</b></span>`
            : ''}
        <span>${streak}</span>
        ${e.red_count?`<span>命中色块 <b>${e.red_count}</b></span>`:''}
        ${e.observations?`<span>观察 <b>${e.observations}</b> 轮</span>`:''}
        ${shown}
      </div>
    </div>
  </div>`;
}

function render(){
  const f = $('feed');
  if(!events.length){ f.innerHTML = '<div class="empty">还没有记录</div>'; return; }
  f.innerHTML = events.map(card).join('');
}

function zoom(src){ $('lbImg').src = src; $('lb').style.display='flex'; }
$('lb').onclick = () => { $('lb').style.display='none'; $('lbImg').src=''; };

async function poll(){
  try{
    const st = await (await fetch('/api/status')).json();
    const env = await (await fetch('/api/environment')).json();
    $('envText').textContent = env.message;
    $('envText').style.color = env.status==='error'?'var(--red)':env.status==='ready'?'var(--green)':'var(--amber)';
    $('btnEnv').style.display = env.status==='error'?'':'none';
    $('btnEnv').disabled = st.running;
    $('statePill').textContent = st.running ? '运行中' : '未运行';
    $('statePill').className = 'pill' + (st.running?' on':'');
    $('fgPill').style.display = (st.running && st.foreground === false) ? '' : 'none';
    $('winSel').disabled = st.running;
    $('btnWin').disabled = st.running;
    $('sStreak').textContent = st.streak;
    $('sTotal').textContent = st.total;
    $('sReds').textContent = events.filter(e=>e.kind==='red').length;
    $('sOcr').textContent = st.ocr_ms ? st.ocr_ms.toFixed(0)+'ms' : '—';
    $('sObs').textContent = st.obs + ' 轮';
    $('sCycle').textContent = st.cycle_avg_ms != null ? st.cycle_avg_ms.toFixed(0)+'ms' : '—';
    $('sItemOcr').textContent = '物品 OCR：' + (st.item_ocr_ms ? st.item_ocr_ms.toFixed(0)+'ms' : '—');
    $('btnStart').disabled = st.running || env.status!=='ready';
    $('btnStop').disabled = !st.running;

    const box = $('errBox');
    if(st.error || commandError){ box.textContent = st.error || commandError; box.style.display='block'; }
    else box.style.display='none';

    if(st.current){
      $('curCard').style.display='';
      $('curText').textContent = st.current.ocr_text || '';
      const nb = st.current.name_bbox;
      $('curMeta').innerHTML =
        (nb && nb.length ? `<span>名字位置 <b>(${nb[0]},${nb[1]})</b></span>` : '') +
        `<span>已观察 <b>${st.current.observations ?? 0}</b> 轮</span>` +
        `<span>${st.misses ? `丢失 <b style="color:var(--amber)">${st.misses}/${st.miss_need}</b>` : ''}</span>` +
        `<span>${st.current.red?'<b style="color:var(--red)">已见红</b>':'暂未见红'}</span>`;
    } else $('curCard').style.display='none';

    if(!runsLoaded){ runsLoaded = true; refreshRuns(); }
    if(!curRun && st.run_dir){ curRun = st.run_dir; sinceSeq = 0; events = []; refreshRuns(); }
    if(st.run_dir && curRun && st.run_dir !== curRun){ curRun = st.run_dir; sinceSeq = 0; events = []; refreshRuns(); }

    const d = await (await fetch(`/api/events?since=${sinceSeq}&run=${encodeURIComponent(curRun||'')}`)).json();
    if(d.events && d.events.length){
      events = events.concat(d.events.map(e => ({...e, run: curRun})));
      sinceSeq = d.last_seq;
      render();
    }
  }catch(err){ /* 服务器没起来时静默重试 */ }
}

async function refreshRuns(){
  try{
    const d = await (await fetch('/api/runs')).json();
    const sel = $('runSel');
    sel.innerHTML = d.runs.map(r =>
      `<option value="${esc(r)}"${r===curRun?' selected':''}>${esc(r)}</option>`).join('');
  }catch(e){}
}

$('runSel').onchange = async () => {
  location.href = '/history?run=' + encodeURIComponent($('runSel').value);
};

async function refreshWindows(){
  try{
    const d = await (await fetch('/api/windows')).json();
    const sel = $('winSel');
    const cur = d.current || '';
    sel.innerHTML = '<option value="">— 选择游戏窗口 —</option>' +
      d.windows.map(w => `<option value="${esc(w.title)}"${w.title===cur?' selected':''}>` +
        `${esc(w.title)}  (${w.w}×${w.h})</option>`).join('');
  }catch(e){}
}

$('winSel').onchange = async () => {
  const title = $('winSel').value;
  const r = await (await fetch('/api/window', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({title})
  })).json();
  if(!r.ok){ commandError=r.error; const b=$('errBox'); b.textContent=r.error; b.style.display='block'; }
  else { commandError=''; const b=$('errBox'); b.style.display='none'; loadPreview(); }
};

async function loadPreview(){
  const t = $('winSel').value || '';
  const img = $('prevImg'), empty = $('prevEmpty');
  if(!t){ empty.style.display=''; empty.textContent='还没选窗口 —— 右上角选一个，然后点【预览】';
          img.style.display='none'; return; }
  empty.style.display=''; empty.textContent='抓取中…';
  img.style.display='none';
  const url = '/api/preview?w=' + encodeURIComponent(t) + '&t=' + Date.now();
  const r = await fetch(url);
  if(!r.ok){
    let msg = r.status;
    try{ msg = (await r.json()).error || msg; }catch(e){}
    empty.textContent = '预览失败：' + msg;
    return;
  }
  const blob = await r.blob();
  img.src = URL.createObjectURL(blob);
  img.style.display = '';
  empty.style.display = 'none';
}

$('btnPrev').onclick = loadPreview;
$('btnWin').onclick = refreshWindows;
async function command(path){
  try{const response=await fetch(path,{method:'POST'});const data=await response.json();
      commandError=response.ok?'':data.error||'操作失败';}
  catch(e){commandError='连接失败：'+e.message;}
  await poll();
}
$('btnStart').onclick = () => command('/api/start');
$('btnStop').onclick = () => command('/api/stop');
$('btnEnv').onclick = () => command('/api/environment/retry');

refreshRuns();
refreshWindows();
poll();
setInterval(poll, 900);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    runner: Runner = None  # type: ignore[assignment]

    def log_message(self, *_a):  # 别把每条请求都打到控制台
        pass

    # -- 工具 --------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # -- 路由 --------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == '/api/info':
            return self._json(server_info(self.runner))

        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

        if u.path == '/history':
            return self._send(200, HISTORY_PAGE.encode('utf-8'), 'text/html; charset=utf-8')

        if u.path == '/api/history':
            try:
                limit = max(1, min(100, int(q.get('limit', ['30'])[0])))
                offset = max(0, int(q.get('offset', ['0'])[0]))
            except ValueError:
                return self._json({'error': '分页参数错误'}, 400)
            return self._json(self.runner.store.list_runs(limit, offset))

        if u.path == '/api/run':
            try:
                run = self.runner.store.run(q.get('run', [''])[0])
            except ValueError:
                return self._json({'error': '运行编号错误'}, 400)
            return self._json(run or {'error': '运行不存在'}, 200 if run else 404)

        if u.path == "/api/status":
            return self._json(self.runner.status())

        if u.path == '/api/environment':
            return self._json(self.runner.environment.status())

        if u.path == "/api/runs":
            runs = self.runner.store.run_names()
            return self._json({"runs": runs})

        if u.path == "/api/windows":
            from capture import list_windows
            ws = [{"title": w["title"], "w": w["w"], "h": w["h"]}
                  for w in sorted(list_windows(min_side=400),
                                  key=lambda x: -(x["w"] * x["h"]))]
            return self._json({"windows": ws, "current": self.runner.window})

        if u.path == "/api/preview":
            return self._preview(q.get("w", [""])[0] or self.runner.window)

        if u.path == "/api/events":
            try:
                since = max(0, int(q.get("since", ["0"])[0]))
            except ValueError:
                return self._json({'error': '序号错误'}, 400)
            name = q.get("run", [""])[0]
            if name and not valid_run(name):
                return self._json({'error': '运行编号错误'}, 400)
            return self._json(self._read_events(name, since))

        if u.path.startswith("/shot/"):
            return self._serve_shot(u.path[len("/shot/"):])

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == '/api/environment/retry':
            if self.runner.status()['running']:
                return self._json({'error': '先停止检测再检查环境'}, 400)
            return self._json({'ok': True, 'started': self.runner.environment.start()})
        if u.path == '/api/review':
            try:
                n = int(self.headers.get('Content-Length', 0))
                if not 0 < n <= 65536:
                    raise ValueError('invalid body size')
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict) or type(body.get('seq')) is not int:
                    raise ValueError('invalid event sequence')
                self.runner.store.review(body.get('run', ''), body['seq'], body.get('label'))
            except (ValueError, TypeError):
                return self._json({'error': '复核参数错误或记录不存在'}, 400)
            return self._json({'ok': True})
        if u.path == "/api/start":
            if not self.runner.window and not self.runner.monitor_only:
                return self._json(
                    {"ok": False, "error": "还没选游戏窗口。先在上面选一个，"
                                           "否则会退化成抓前台窗口（=浏览器）"}, 400)
            started = self.runner.start()
            if not started and self.runner.status().get('error'):
                return self._json({'ok': False, 'error': self.runner.status()['error']}, 500)
            return self._json({"ok": True, "started": started})
        if u.path == "/api/stop":
            self.runner.stop()
            return self._json({"ok": True})
        if u.path == "/api/window":
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": "bad json"}, 400)
            if self.runner.status()["running"]:
                return self._json({"ok": False, "error": "运行中不能改，先停止"}, 400)
            self.runner.window = body.get("title", "")
            # 存下来，这样命令行工具（name / track / box）也能直接用，
            # 不用靠 foreground_window()（从 bat 跑时那是控制台自己）
            from capture import load_settings, save_settings
            s = load_settings()
            s["window"] = self.runner.window
            save_settings(s)
            return self._json({"ok": True, "window": self.runner.window})
        self._json({"error": "not found"}, 404)

    # -- 预览 --------------------------------------------------------------
    def _preview(self, title: str):
        """
        抓一帧回来给用户看，并把工作区框画上去。

        为什么需要这个：实测用户把窗口选成了 Edge（浏览器里放着 B 站视频），
        工具就对着浏览器画面跑完了整条链路 —— OCR 认出视频里的「机密保险箱」、
        色块检测命中视频里的红品，一切"正常"，但全是错的。
        选错窗口不只是没数据，是**会产出看着合理的错数据**，所以必须让人眼确认。
        """
        import cv2
        from capture import Grabber

        if not title:
            return self._json({"error": "没选窗口"}, 400)
        try:
            # allow_fallback 保持默认 True：用浏览器放录像当输入源是正当用法
            g = Grabber(window_substr=title)
            if g.window is None:
                g.close()
                return self._json({"error": f"找不到标题含 {title!r} 的窗口"}, 404)
            frame = g.grab()
            g.close()
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"抓屏失败: {e}"}, 500)

        h, w = frame.shape[:2]
        # 把工作区框画上去，这样框选对不对一眼能看出来
        if self.runner.cfg.work_region:
            from roi import to_pixels
            x, y, rw, rh = to_pixels(tuple(self.runner.cfg.work_region), frame.shape)
        else:
            x = int(round(w * (1.0 - self.runner.cfg.right_frac)))
            y, rw, rh = 0, w - x, h
        cv2.rectangle(frame, (x, y), (x + rw, y + rh), (0, 255, 255), 4)
        cv2.putText(frame, "WORK REGION", (x + 8, y + 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)

        scale = min(560.0 / w, 1.0)
        if scale < 1.0:
            frame = cv2.resize(frame, None, fx=scale, fy=scale,
                               interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            return self._json({"error": "编码失败"}, 500)
        return self._send(200, buf.tobytes(), "image/jpeg")

    # -- 数据 --------------------------------------------------------------
    def _read_events(self, run: str, since: int) -> dict:
        """读某个 run 的 events.jsonl。run 为空表示当前运行。"""
        name = run or (self.runner.run_dir.name if self.runner.run_dir else '')
        return self.runner.store.events(name, since) if name else {'events': [], 'last_seq': 0}

    def _serve_shot(self, rel: str):
        # 只允许 runs/<dir>/<file>，挡住路径穿越
        from pathlib import Path
        from urllib.parse import unquote
        parts = unquote(rel).split('/')
        if len(parts) != 2 or not valid_run(parts[0]) or not parts[1] \
                or Path(parts[1]).name != parts[1] or '\\' in parts[1] or ':' in parts[1]:
            return self._json({"error": "bad path"}, 400)
        run, name = parts
        root = self.runner.store.root.resolve()
        path = (root / run / name).resolve()
        if not path.is_relative_to(root):
            return self._json({'error': 'bad path'}, 400)
        if not path.is_file():
            return self._json({"error": "no such file"}, 404)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)


# --------------------------------------------------------------------------
DEFAULT_PORT = 17833        # 别用 8765 —— 实测在本机被 soundradar.exe 占着


def _is_our_server(host: str, port: int, expected: dict) -> bool:
    """
    这个端口上跑的是不是**本工具自己**的服务？

    为什么必须查这个：Python 的 HTTPServer 设了 allow_reuse_address，
    所以**多个进程能绑同一个端口**，浏览器连上去碰到哪个是随机的。
    实测用户双击了几次 bat，就有三个 serve 同时在 17833 上，
    其中一个是改之前的旧版本 —— 于是"明明改好了却还是旧行为"，
    刷新几次表现还不一样。必须先探测再决定，不能闷头再起一个。
    """
    import urllib.error
    import urllib.request
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/api/info"
    try:
        with urllib.request.urlopen(url, timeout=1.0) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d == expected
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def serve(runner: Runner, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          open_browser: bool = True, try_next: int = 10) -> None:
    """
    起服务。

    已经有一个本工具的服务在跑时**直接复用**，不再起第二个 ——
    两个实例抢同一个端口会让浏览器随机连到其中一个，行为看着忽好忽坏。
    端口被别的程序占才往后顺延。
    """
    Handler.runner = runner
    expected = server_info(runner)
    _show = "127.0.0.1" if host in ("0.0.0.0", "") else host

    for p in range(port, port + try_next):
        if _is_our_server(host, p, expected):
            print(f"\n  端口 {p} 上已经有一个本工具的服务在跑，直接用它。")
            print(f"  没有另起新实例 —— 两个实例抢同一端口会让页面行为忽好忽坏。")
            print(f"\n  Web UI:  http://{_show}:{p}/")
            print(f"\n  注意：如果你刚改过代码，那个服务跑的是**启动时的版本**，")
            print(f"        得先在它的控制台窗口按 Ctrl+C 关掉，再重新启动。")
            runner.stop()
            if open_browser:
                webbrowser.open(f"http://{_show}:{p}/")
            return

    httpd = None
    last_err = None
    for p in range(port, port + try_next):
        try:
            httpd = ExclusiveHTTPServer((host, p), Handler)
            if p != port:
                print(f"\n  端口 {port} 被别的程序占用，已自动改用 {p}")
            port = p
            break
        except OSError as e:
            last_err = e

    if httpd is None:
        print(f"\n  端口 {port}~{port + try_next - 1} 全被占用了，没法启动。")
        print(f"  最后一个错误: {last_err}")
        print(f"  换一个再试:   python main.py serve --port 20000")
        runner.stop()
        return

    url = f"http://{_show}:{port}/"
    print(f"\n  Web UI:  {url}")
    if host == "0.0.0.0":
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            print(f"  局域网:  http://{s.getsockname()[0]}:{port}/   (手机也能开)")
            s.close()
        except OSError:
            pass
        print("  注意: 绑了 0.0.0.0，同网段的人都能看到你的屏幕截图。")
    print("  Ctrl+C 退出\n")
    from environment import Environment, verify_python
    runner.environment = Environment(verify=verify_python)
    runner.environment.start()
    if open_browser and host != "0.0.0.0":
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        runner.stop()
        httpd.server_close()
