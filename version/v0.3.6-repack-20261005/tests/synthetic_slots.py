"""Generate reproducible 1..6-slot geometry/colour fixtures and replay Rust CLI.

Developer dependency: Pillow. No game screenshots or private data are used.
Run: python tests/synthetic_slots.py --exe path/to/ab-red-detect.exe
Add --strict to exit nonzero if any expected classification differs.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import argparse
import hashlib
import json
import random
import subprocess
import time
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
SEED=20261005
RECTANGLES=[(w,h) for n in range(1,7) for w in range(1,n+1) for h in range(1,n+1) if w*h==n]
COLORS={'dark':(55,26,22),'normal':(115,49,42),'selected':(74,57,56),'bright':(245,212,215)}
NEGATIVES=['L','T','U','cross','triangle','circle','diagonal','scatter','outline',
    'icon_on_green','floating_rectangle','orange','gold','green','title_only']

def font(size):
    return ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',size)

def generate(out):
    out.mkdir(parents=True,exist_ok=True)
    cases=[]
    specifications=[]
    for cell in [48,64,85]:
        for colour in COLORS:
            for w,h in RECTANGLES:
                for pattern in ['plain','artwork']:
                    specifications.append((cell,colour,w,h,pattern,True))
            for shape in NEGATIVES:
                specifications.append((cell,colour,3,3 if shape in ['cross','scatter'] else 2,shape,False))
    for index,(cell,colour,w,h,shape,want) in enumerate(specifications):
        label=f'{index:04}_{cell}_{colour}_{w}x{h}_{shape}'
        rng=random.Random(SEED+index)
        im=Image.new('RGB',(cell*7+68,cell*7+112),(19,20,21));d=ImageDraw.Draw(im)
        f=font(max(12,round(cell*.25)))
        tb=d.textbbox((34,24),'电子保险箱',font=f)
        d.rectangle((tb[0]-5,tb[1]-4,tb[2]+5,tb[3]+4),fill=(70,28,25))
        d.text((34,24),'电子保险箱',font=f,fill=(235,235,235))
        anchor=[tb[0],tb[1],tb[2]-tb[0],tb[3]-tb[1]]
        gx=33;gy=tb[3]+round(cell*.55)
        d.rectangle((gx,gy,gx+7*cell,gy+7*cell),fill=(23,23,23))
        x=gx+1;y=gy+1;right=gx+w*cell-1;bottom=gy+h*cell-1
        red=COLORS[colour]
        bbox=[x,y,right-x+1,bottom-y+1]
        if want:
            d.rectangle((x,y,right,bottom),fill=red)
            if shape=='artwork':
                # An irregular item icon inside a rectangular rarity background.
                # Keep the footprint unchanged; never equate icon shape with rarity.
                bw=right-x;bh=bottom-y
                d.ellipse((x+bw*.18,y+bh*.22,x+bw*.65,y+bh*.76),fill=(140,146,140))
                d.polygon([(x+bw*.30,y+bh*.16),(x+bw*.80,y+bh*.65),(x+bw*.66,y+bh*.81)],fill=(36,50,45))
                d.line((x+bw*.29,y+bh*.25,x+bw*.70,y+bh*.68),fill=(213,210,180),width=max(2,cell//12))
        else:
            # Most negative bounding boxes span six slots. The five-cell cross
            # additionally tests a sparse footprint whose bounding box spans nine.
            r=gx+w*cell-1;b=gy+h*cell-1
            if shape=='L':
                d.rectangle((x,y,gx+cell-1,b),fill=red);d.rectangle((x,gy+(h-1)*cell+1,r,b),fill=red)
            elif shape=='T':
                d.rectangle((x,y,r,gy+cell-1),fill=red);d.rectangle((gx+cell+1,y,gx+2*cell-1,b),fill=red)
            elif shape=='U':
                for box in [(x,y,gx+cell-1,b),(gx+2*cell+1,y,r,b),(x,gy+(h-1)*cell+1,r,b)]:d.rectangle(box,fill=red)
            elif shape=='cross':
                d.rectangle((gx+cell+1,y,gx+2*cell-1,b),fill=red);d.rectangle((x,gy+cell+1,r,gy+2*cell-1),fill=red)
            elif shape=='triangle':d.polygon([(x,y),(r,b),(x,b)],fill=red)
            elif shape=='circle':d.ellipse((x,y,r,b),fill=red)
            elif shape=='diagonal':d.polygon([(x,y),(x+cell,y),(r,b-cell),(r,b),(r-cell,b),(x,y+cell)],fill=red)
            elif shape=='scatter':
                # Leave an empty row AND column between isolated single cells.
                # The former 3x2 layout was two valid vertical 1x2 rectangles,
                # indistinguishable from two real two-slot items in a screenshot.
                for ox,oy in [(0,0),(2,0),(0,2),(2,2)]:d.rectangle((x+ox*cell,y+oy*cell,gx+(ox+1)*cell-1,gy+(oy+1)*cell-1),fill=red)
            elif shape=='outline':d.rectangle((x,y,r,b),outline=red,width=max(2,cell//14))
            elif shape=='icon_on_green':
                d.rectangle((x,y,r,b),fill=(29,56,29));margin=cell*.24
                d.polygon([(x+margin,y+margin),(r-margin,y+cell*.8),(r-cell*.7,b-margin),(x+cell*.6,b-cell*.4)],fill=red)
            elif shape=='floating_rectangle':
                shift=round(cell*.34);d.rectangle((x+shift,y+shift,r+shift,b+shift),fill=red)
            elif shape in ['orange','gold','green']:
                c={'orange':(90,70,64),'gold':(130,92,39),'green':(29,56,29)}[shape];d.rectangle((x,y,r,b),fill=c)
        for xx in range(gx,gx+7*cell+1,cell):d.line((xx,gy,xx,gy+7*cell),fill=(82,82,82))
        for yy in range(gy,gy+7*cell+1,cell):d.line((gx,yy,gx+7*cell,yy),fill=(82,82,82))
        # Small deterministic noise in the artwork region models compression.
        if want and shape=='artwork':
            for _ in range(max(20,w*h*cell//5)):
                px=rng.randrange(x,right+1);py=rng.randrange(y,bottom+1)
                p=im.getpixel((px,py));delta=rng.choice([-2,-1,1,2]);im.putpixel((px,py),tuple(max(0,min(255,c+delta)) for c in p))
        file=out/(label+'.png');im.save(file)
        cases.append({'id':label,'file':file.name,'cell':cell,'colour':colour,'slots':w*h if want else None,
            'width_slots':w,'height_slots':h,'shape':shape,'expected_red':want,'anchor':anchor,
            'expected_bbox':bbox if want else None,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
    (out/'cases.json').write_text(json.dumps({'seed':SEED,'cases':cases},ensure_ascii=False,indent=2),encoding='utf-8')
    return cases

def iou(a,b):
    area=max(0,min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]))*max(0,min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1]))
    return area/(a[2]*a[3]+b[2]*b[3]-area)

def replay(exe,out,case):
    start=time.perf_counter()
    process=subprocess.run([str(exe),'--panel',str(out/case['file']),','.join(map(str,case['anchor']))],capture_output=True,encoding='utf-8',timeout=30,creationflags=0x08000000)
    if process.returncode:raise RuntimeError(case['id']+': '+process.stderr)
    data=json.loads(process.stdout);got=bool(data['red']);score=max((iou(b['bbox'],case['expected_bbox']) for b in data['red']),default=0.) if case['expected_red'] else None
    return {**case,'actual_red':got,'classification_pass':got==case['expected_red'],
        'footprint_iou':score,'footprint_pass':score>=.85 if score is not None else None,
        'duration_ms':round((time.perf_counter()-start)*1000,2),'candidates':data['candidates']}

def sheet(out,results,name,selected):
    cols=7;tw=210;th=222
    canvas=Image.new('RGB',(cols*tw,((len(selected)+cols-1)//cols)*th+55),(16,18,22));d=ImageDraw.Draw(canvas)
    d.text((15,12),name,font=font(21),fill='white')
    for i,c in enumerate(selected):
        x=(i%cols)*tw;y=(i//cols)*th+55
        im=Image.open(out/c['file']);a=c['anchor'];cell=c['cell']
        im=im.crop((15,a[1]-7,min(im.width,33+max(3,c['width_slots'])*cell+20),min(im.height,a[1]+a[3]+cell*.55+max(3,c['height_slots'])*cell+20)))
        im.thumbnail((195,166));canvas.paste(im,(x+7,y+3))
        ok=c['classification_pass'];text=f"{c['width_slots']}×{c['height_slots']} {c['shape']}"
        d.text((x+7,y+170),text,font=font(15),fill='white')
        partial=ok and c['expected_red'] and not c['footprint_pass']
        text='检出·框不完整' if partial else '通过' if ok else '漏报' if c['expected_red'] else '误报'
        d.text((x+7,y+191),text,font=font(16),fill=(243,198,124) if partial else (96,211,161) if ok else (255,110,110))
    path=out/(name+'.png');canvas.save(path);return path

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path,default=ROOT/'native/target/release/ab-red-detect.exe');parser.add_argument('--out',type=Path,default=ROOT/'tests/synthetic-slots');parser.add_argument('--strict',action='store_true');args=parser.parse_args()
    cases=generate(args.out);print(f'Generated {len(cases)} pixel-exact cases; seed={SEED}',flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda c:replay(args.exe,args.out,c),cases))
    counts=Counter(('positive' if c['expected_red'] else 'negative',c['classification_pass']) for c in results)
    failures=[c['id'] for c in results if not c['classification_pass']]
    clipped=[c['id'] for c in results if c['expected_red'] and c['actual_red'] and not c['footprint_pass']]
    summary={'seed':SEED,'total':len(results),'positive_total':sum(c['expected_red'] for c in results),
        'positive_pass':counts['positive',True],'negative_total':sum(not c['expected_red'] for c in results),
        'negative_pass':counts['negative',True],'failure_count':len(failures),'partial_footprints':len(clipped),
        'failures_by_shape':dict(Counter(c['shape'] if not c['expected_red'] else f"{c['width_slots']}x{c['height_slots']}" for c in results if not c['classification_pass']))}
    (args.out/'results.json').write_text(json.dumps({'summary':summary,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
    positive=[next(c for c in results if c['expected_red'] and c['cell']==85 and c['colour']=='normal' and c['width_slots']==w and c['height_slots']==h and c['shape']=='artwork') for w,h in RECTANGLES]
    negative=[next(c for c in results if not c['expected_red'] and c['cell']==85 and c['colour']=='normal' and c['shape']==s) for s in NEGATIVES]
    sheet(args.out,results,'正例-矩形背景与不规则物品图案',positive)
    sheet(args.out,results,'反例-非矩形与非红底',negative)
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    if args.strict and failures:return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
