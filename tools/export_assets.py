from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
import webui,history_ui
extra='''<script>const quit=document.createElement("button");quit.textContent="退出程序";quit.onclick=()=>fetch("/api/quit",{method:"POST"}).then(()=>document.body.innerHTML="<p>程序已退出，可以关闭此页。</p>");document.querySelector("header").append(quit);</script>'''
(root/'native/assets/index.html').write_text(webui.PAGE.replace('</body>',extra+'</body>'),encoding='utf-8')
(root/'native/assets/history.html').write_text(history_ui.PAGE,encoding='utf-8')
