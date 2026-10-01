from pathlib import Path
import subprocess,json,sys,tempfile,time,urllib.request,urllib.error
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from test_real import CASES
exe=root/'native/target/release/ab-red-detect.exe'
for path,tag,want,panel in CASES:
    result=subprocess.run([str(exe),'--analyze',str(root/path)],capture_output=True,encoding='utf-8',timeout=30)
    if result.returncode:raise RuntimeError(result.stderr)
    data=json.loads(result.stdout);got=data['current'] is not None
    assert got==panel,(path,got,panel)
    red=bool(data['current'] and data['current']['red'])
    assert red==bool(want),(path,red,want,data)
    print('PASS native sample',path,'red=',red,flush=True)
with tempfile.TemporaryDirectory() as directory:
    with subprocess.Popen([str(exe),'--data-dir',directory,'--port','17948','--no-browser'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=0x08000000) as server:
        def api(path,body=None):
            request=urllib.request.Request('http://127.0.0.1:17948'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
            return json.load(urllib.request.urlopen(request,timeout=5))
        try:
            for _ in range(30):
                try:api('/api/status');break
                except (OSError,urllib.error.URLError):time.sleep(.1)
            assert api('/api/history')['total']==0
            try:api('/api/start',{})
            except urllib.error.HTTPError as e:assert e.code==400
            else:raise AssertionError('started without a target')
            for path in ['/api/events?run=..%2Foutside','/shot/..%2Foutside/x.png']:
                try:api(path)
                except urllib.error.HTTPError as e:assert e.code==400
                else:raise AssertionError('invalid path accepted')
            print('PASS native HTTP and empty archive',flush=True)
        finally:server.terminate();server.wait(timeout=5)
    # Exercise the actual executable across restart, with legacy JSONL evidence.
    runs=Path(directory)/'runs';legacy=runs/'legacy';legacy.mkdir(parents=True,exist_ok=True)
    (legacy/'events.jsonl').write_text('{"seq":1,"kind":"red","t":"2026-09-30T20:00:00","text":"保险箱","shots":["item.png"]}\n{"seq":',encoding='utf-8')
    (legacy/'item.png').write_bytes((root/'tests/fixtures/solar_false_positive.png').read_bytes())
    for restart in range(2):
        server=subprocess.Popen([str(exe),'--data-dir',directory,'--port','17948','--no-browser'],creationflags=0x08000000)
        try:
            for _ in range(50):
                try:api('/api/history');break
                except (OSError,urllib.error.URLError):time.sleep(.1)
            run=api('/api/run?run=legacy')
            assert run['total']==1 and run['counts']['red']==1,run
            assert urllib.request.urlopen('http://127.0.0.1:17948/shot/legacy/item.png').status==200
            if restart==0:api('/api/review',{'run':'legacy','seq':1,'label':'clean'})
            else:
                assert api('/api/events?run=legacy')['events'][0]['review']=='clean'
                assert api('/api/run?run=legacy')['false_positive']==1
        finally:server.terminate();server.wait(timeout=5)
    print('PASS native legacy import, screenshots, review after restart',flush=True)
