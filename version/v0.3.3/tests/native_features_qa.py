"""Validate Rust 0.3.3 statistics and ZIP export using independent stdlib readers."""
from pathlib import Path
import io
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

root = Path(__file__).resolve().parents[1]
exe = Path(sys.argv[1]) if len(sys.argv)>1 else root/'native/target/release/ab-red-detect.exe'
port = 17966

def request(path, body=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=None if body is None else json.dumps(body).encode(), headers={'Content-Type':'application/json'})
    return urllib.request.urlopen(req, timeout=30)

def api(path, body=None):
    with request(path,body) as response:
        return json.load(response)

def launch(base):
    process = subprocess.Popen([str(exe),'--data-dir',str(base),'--port',str(port),'--no-browser'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
    for _ in range(120):
        if process.poll() is not None:raise AssertionError(process.communicate())
        try:
            if api('/api/environment')['status']=='ready':return process
        except OSError:pass
        time.sleep(.1)
    process.terminate();process.wait(10)
    raise AssertionError('startup timeout')

def close(process):
    api('/api/quit',{})
    process.wait(10)

with tempfile.TemporaryDirectory(prefix='native-features-',dir=root/'tests') as directory:
    base=Path(directory)
    expected_trace=(b'{"state":"SESSION","cycle_ms":20}\n'*40000)
    png=(root/'tests/fixtures/solar_false_positive.png').read_bytes()
    cases={'run_a':['clean','clean']+['red']*8+['clean','clean'],
           'run_b':['red']*5+['incomplete']+['red']*5,
           'run_c':['clean']*10,'run_d':['red']*9}
    for name,kinds in cases.items():
        folder=base/'runs'/name;folder.mkdir(parents=True)
        events=[{'seq':i+1,'kind':kind,'text':'保险箱','t':f'2026-10-01T12:00:{i:02}','shots':['evidence.png']} for i,kind in enumerate(kinds)]
        (folder/'events.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events)+'{"seq":',encoding='utf-8')
        (folder/'trace.jsonl').write_bytes(expected_trace)
        (folder/'evidence.png').write_bytes(png)
    process=launch(base)
    try:
        assert api('/api/info')['version']=='0.3.3'
        best=api('/api/stats')['best_ten']
        assert (best['run'],best['red_rate'],best['start_seq'],best['end_seq'])==('run_a',80,3,12),best
        api('/api/review',{'run':'run_a','seq':3,'label':'clean'})
        assert api('/api/stats')['best_ten']==best
        export=api('/api/export?run=run_a',{})
        with request(export['download_url']) as response:
            assert response.headers['Content-Type']=='application/zip'
            assert export['filename'] in response.headers['Content-Disposition']
            data=response.read()
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            assert package.testzip() is None
            assert package.read('run/evidence.png')==png
            assert package.read('run/trace.jsonl')==expected_trace
            snapshot=json.loads(package.read('run.json'))
            assert snapshot['id']=='run_a' and len(snapshot['events'])==12
            assert snapshot['events'][2]['review']=='clean'
            assert json.loads(package.read('diagnostics.json'))['version']=='0.3.3'
            assert json.loads(package.read('personal_stats.json'))['best_ten']==best
            assert not any('run_b' in name or name.endswith('.sqlite3') for name in package.namelist())
            assert len(data)<len(expected_trace)//3
        assert (base/'exports'/export['filename']).read_bytes()==data
        for path in ['/api/export?run=..%2Foutside','/feedback/..%2Foutside.zip','/feedback/not-a-feedback.zip']:
            try:
                with request(path) as response:response.read()
            except urllib.error.HTTPError as error:assert error.code==400
            else:raise AssertionError('unsafe export path accepted')
        for path,mime in [('/assets/app.css','text/css'),('/assets/app.js','text/javascript')]:
            with request(path) as response:assert mime in response.headers['Content-Type']
        print('PASS Rust API: rolling ten, incomplete/run boundaries, review isolation, ZIP CRC, compressed trace, screenshots and safe paths',flush=True)
    finally:close(process)
    process=launch(base)
    try:
        assert api('/api/stats')['best_ten']==best
        assert api('/api/events?run=run_a')['events'][2]['review']=='clean'
        print('PASS personal best and reviews persist after restart',flush=True)
    finally:close(process)
    empty=base/'empty';empty.mkdir()
    process=launch(empty)
    try:
        assert api('/api/stats')['best_ten'] is None
        export=api('/api/export',{})
        with request(export['download_url']) as response, zipfile.ZipFile(io.BytesIO(response.read())) as package:
            assert package.testzip() is None
            assert 'diagnostics.json' in package.namelist()
            assert 'run.json' not in package.namelist()
        assert api('/api/history')['total']==0
        print('PASS no-history environment feedback ZIP',flush=True)
    finally:close(process)
