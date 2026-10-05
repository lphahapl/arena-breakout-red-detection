"""Replay real Rust panel detection for two-slot backgrounds and single-slot rejection."""
from pathlib import Path
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from synthetic_slots import generate, replay


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path, default=root/'native/target/release/ab-red-detect.exe')
    parser.add_argument('--out', type=Path, default=root/'tests/synthetic-slots/two-slot-regression')
    args = parser.parse_args()
    cases = generate(args.out)
    selected = [c for c in cases if c['expected_red'] and c['slots'] in (1, 2)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda c: replay(args.exe, args.out, c), selected))
    failed = []
    for case in results:
        want = case['slots'] == 2
        if case['actual_red'] != want or (want and not case['footprint_pass']):
            failed.append({'id': case['id'], 'want': want, 'actual': case['actual_red'],
                           'iou': case['footprint_iou'], 'candidates': case['candidates']})
    report = {'total': len(results), 'horizontal_two': sum(c['width_slots'] == 2 for c in results),
              'vertical_two': sum(c['height_slots'] == 2 for c in results),
              'single_rejected': sum(c['slots'] == 1 and not c['actual_red'] for c in results),
              'failed': failed}
    (args.out/'regression.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({**report, 'failed': [c['id'] for c in failed]}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
