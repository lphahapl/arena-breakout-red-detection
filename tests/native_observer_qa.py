"""Real screenshot/OCR -> game-mode gate -> SQLite API regression.

Windows, Chinese OCR and developer Python/Tk required. Uses an owned synthetic
game window; no game account, private screenshot or existing history is touched.
Optional --preview keeps the spectator fixture and local UI up for visual QA.
"""
from pathlib import Path
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

if '--fixture' in sys.argv:
    import tkinter as tk
    win=tk.Tk();win.title(sys.argv[-1]);win.geometry('1500x900+10+10')
    win.attributes('-topmost',True)
    canvas=tk.Canvas(win,bg='#222222',highlightthickness=0);canvas.pack(fill='both',expand=True)
    commands=queue.Queue()
    def read():
        for line in sys.stdin: commands.put(line.strip())
    threading.Thread(target=read,daemon=True).start()
    state={'mode':'watch','box':True}
    def paint():
        canvas.delete('all')
        if state['box']:
            canvas.create_text(944,48,text='电子保险箱',fill='white',font=('Microsoft YaHei UI',18),anchor='nw')
            canvas.create_rectangle(933,95,1233,455,fill='#171717',outline='')
            canvas.create_rectangle(934,96,1052,214,fill='#5a2d2d',outline='')
            for x in range(933,1234,60):canvas.create_line(x,95,x,455,fill='#606060')
            for y in range(95,456,60):canvas.create_line(933,y,1233,y,fill='#606060')
        text={'watch':'观战中','end':'结算','none':''}[state['mode']]
        canvas.create_rectangle(560,828,940,886,fill='#111111',outline='')
        canvas.create_text(750,858,text=text,fill='#eeeeee',font=('Microsoft YaHei UI',22))
    def tick():
        changed=False
        while not commands.empty():
            command=commands.get()
            if command in ('watch','end','none'):state['mode']=command
            elif command in ('show','hide'):state['box']=command=='show'
            changed=True
        if changed:paint()
        win.after(25,tick)
    paint();tick();win.mainloop();sys.exit()

root=Path(__file__).resolve().parents[1]
exe=root/'native/target/release/ab-red-detect.exe'
port=17973
title='ABR observer fixture '+str(time.time_ns())
preview='--preview' in sys.argv
def api(path,body=None):
    req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,
        data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)

fixture=None;process=None
with tempfile.TemporaryDirectory() as directory:
    base=Path(directory)
    try:
        fixture=subprocess.Popen([sys.executable,__file__,'--fixture',title],stdin=subprocess.PIPE,
            text=True,encoding='utf-8',creationflags=0x08000000)
        (base/'settings.json').write_text(json.dumps({'window':title}),encoding='utf-8')
        with (base/'out.txt').open('w') as out,(base/'err.txt').open('w') as err:
            process=subprocess.Popen([str(exe),'--data-dir',directory,'--port',str(port),'--no-browser'],
                stdout=out,stderr=err,creationflags=0x08000000)
        def wait(predicate,description,timeout=25):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                assert process.poll() is None,(base/'err.txt').read_text(errors='replace')
                try:
                    value=predicate()
                    if value:return value
                except OSError:pass
                time.sleep(.1)
            raise AssertionError(description+'; '+str(api('/api/status')))
        def status():
            s=api('/api/status');assert not s['error'],s;return s
        def command(text):fixture.stdin.write(text+'\n');fixture.stdin.flush();time.sleep(.15)
        wait(lambda:api('/api/environment')['status']=='ready','OCR ready')
        wait(lambda:any(w['title']==title for w in api('/api/windows')['windows']),'owned fixture visible')
        assert api('/api/start',{})['started']
        wait(lambda:status()['play_state']=='SPECTATING','spectator gate')
        run=status()['run_dir']
        def events():return api('/api/events?run='+run)['events']
        def counts():return api('/api/run?run='+run)
        def assert_no_safes():
            time.sleep(1.2);assert events()==[],events();assert counts()['total']==0
        assert_no_safes()
        command('hide');command('show');assert_no_safes()
        command('end');wait(lambda:status()['play_state']=='SETTLEMENT','either settlement keyword gates')
        assert_no_safes()
        print('PASS spectator / settlement with visible safe: no opening events or totals',flush=True)
        command('none')
        wait(lambda:status()['current'] and status()['current']['red'],'normal mode resumes and confirms red')
        command('hide')
        wait(lambda:counts()['total']==1,'one own safe settles')
        assert counts()['counts']['red']==1,counts()
        command('show');wait(lambda:status()['current'] is not None,'second own safe starts')
        command('watch')
        wait(lambda:status()['play_state']=='SPECTATING' and status()['current'] is None,'pause active observation')
        wait(lambda:counts()['counts']['incomplete']==1,'active safe becomes incomplete')
        before=len(events());command('hide');command('show');time.sleep(1.2)
        assert len(events())==before and counts()['total']==1,(events(),counts())
        assert [e['kind'] for e in events()]==['start','red','start','incomplete'],events()
        print('PASS automatic resume; active-to-spectator interruption excluded from red rate',flush=True)
        s=status();print('PASS mode OCR average ms:',round(s['mode_ocr_ms'],1),flush=True)
        if preview:
            print(f'PREVIEW http://127.0.0.1:{port}/',flush=True)
            input('Press Enter after UI review to finish.\n')
        api('/api/stop',{});wait(lambda:not status()['running'],'stop')
        assert counts()['metrics']['mode_ocr_calls']>0
    finally:
        if process and process.poll() is None:
            try:api('/api/quit',{})
            except OSError:pass
            try:process.wait(5)
            except subprocess.TimeoutExpired:process.terminate();process.wait(5)
        if fixture and fixture.poll() is None:fixture.terminate();fixture.wait(5)
