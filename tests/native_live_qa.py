"""Exercise environment-thread -> live worker -> restart with a real test window.

Windows and working Chinese OCR required. Python/Tk are developer-only test tools.
The fixture is visible briefly; all detection data goes to a temporary directory.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

root = Path(__file__).resolve().parents[1]
exe = Path(sys.argv[1]) if len(sys.argv)>1 else root/'native/target/release/ab-red-detect.exe'
port = 17969
title = 'ABR regression fixture '+str(time.time_ns())

def api(path, body=None):
    req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,
        data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=5) as response:
        return json.load(response)

fixture=None
process=None
with tempfile.TemporaryDirectory() as directory:
    base=Path(directory)
    try:
        fixture=subprocess.Popen([sys.executable,'-c',
            "import tkinter as t,sys; w=t.Tk(); w.title(sys.argv[1]); w.geometry('800x600+20+20'); "
            "w.attributes('-topmost',True); w.configure(bg='#222222'); "
            "t.Label(w,text='OCR regression fixture',bg='#222222',fg='white',font=('Arial',24)).pack(pady=50); w.mainloop()",title],creationflags=0x08000000)
        (base/'settings.json').write_text(json.dumps({'window':title}),encoding='utf-8')
        with (base/'stdout.txt').open('w') as out,(base/'stderr.txt').open('w') as err:
            process=subprocess.Popen([str(exe),'--data-dir',directory,'--port',str(port),'--no-browser'],stdout=out,stderr=err,creationflags=0x08000000)

        def alive():
            code=process.poll()
            if code is not None:
                raise AssertionError(f'Backend exited: {hex(code&0xffffffff)}; '+(base/'stderr.txt').read_text(errors='replace'))

        def wait_for(predicate, description, timeout=20):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                alive()
                try:
                    result=predicate()
                    if result:return result
                except OSError:
                    alive()
                time.sleep(.1)
            raise AssertionError('Timed out: '+description)

        def ready():
            env=api('/api/environment')
            assert env['status']!='error',env
            return env['status']=='ready'

        wait_for(ready,'Chinese OCR environment')
        wait_for(lambda:any(w['title']==title for w in api('/api/windows')['windows']),'fixture window')
        # Let the short-lived preparation thread finish before invoking cached factories.
        time.sleep(.5)
        for cycle in range(3):
            assert api('/api/start',{})['started']
            def observed():
                state=api('/api/status')
                assert not state['error'],state['error']
                return state['running'] and state['obs']>=3
            wait_for(observed,'actual screenshot/OCR progress')
            with ThreadPoolExecutor(max_workers=3) as pool:
                for _ in range(4):
                    results=list(pool.map(api,['/api/status','/api/history','/api/stats']))
                    assert results[0]['running']
                    assert results[1]['total']==cycle+1
                    alive()
            api('/api/stop',{})
            wait_for(lambda:not api('/api/status')['running'],'clean stop')
            assert not api('/api/status')['error']
            # Exercise another preparation thread, then another detection worker.
            api('/api/environment/retry',{})
            wait_for(ready,'OCR recheck')
            time.sleep(.2)
        histories=api('/api/history')['runs']
        assert len(histories)==3 and all(r['status']=='stopped' for r in histories),histories
        print('PASS three real capture/OCR starts, concurrent status/history/stats, stops and environment retries',flush=True)
    finally:
        if process and process.poll() is None:
            try:api('/api/quit',{})
            except OSError:pass
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
        if fixture and fixture.poll() is None:
            fixture.terminate();fixture.wait(timeout=5)
