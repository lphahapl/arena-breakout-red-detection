"""Check full vertical 1x3 footprint, not merely whether any red was seen."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from synthetic_slots import generate, replay


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path,default=root/'native/target/release/ab-red-detect.exe')
    parser.add_argument('--out',type=Path,default=root/'tests/synthetic-slots/vertical-three-regression')
    args=parser.parse_args()
    jobs=[]
    for row in [0,1,2]:
        for scale in [.18,.25]:
            out=args.out/f'row{row}-title{scale}'
            cases=generate(out,row_offset=row,title_scale=scale,only_positive_shape=(1,3))
            jobs.extend((out,c,row,scale) for c in cases if c['expected_red'] and c['width_slots']==1 and c['height_slots']==3)
    def check(job):
        out,c,row,scale=job
        result=replay(args.exe,out,c)
        return {**result,'row_offset':row,'title_scale':scale}
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(check,jobs))
    failed=[c for c in results if not c['actual_red'] or c['footprint_iou']<.98]
    report={'total':len(results),'pass':len(results)-len(failed),
            'min_iou':min(c['footprint_iou'] for c in results),
            'failed':[{'id':c['id'],'row':c['row_offset'],'title_scale':c['title_scale'],
                       'iou':c['footprint_iou'],'candidates':c['candidates']} for c in failed]}
    args.out.mkdir(parents=True,exist_ok=True)
    (args.out/'regression.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({**report,'failed':len(failed)},ensure_ascii=False))
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
