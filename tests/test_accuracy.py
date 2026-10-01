"""Replay real panels and state-machine failure patterns, without screen capture."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from detect import Config, find_red_blobs
from imgio import imread_u
from track import Tracker


class AccuracyTests(unittest.TestCase):
    def test_item_ocr_waits_for_second_frame_and_exclusion_still_applies(self):
        tr=Tracker(Config());tr._anchor=(10,20,100,21)
        tr.session={'red':False,'red_blobs':[]}
        def candidates(*args,**kwargs):
            return {'blobs':[{'bbox':(30,100,80,150),'accepted':True}]}
        with patch('track.find_red_blobs',side_effect=candidates),patch.object(tr,'_filter_by_text',return_value=[]) as check:
            tr._scan_red(np.zeros((500,800,3),np.uint8))
            self.assertEqual(check.call_args.args[1],[])
            tr._scan_red(np.zeros((500,800,3),np.uint8))
            self.assertEqual(len(check.call_args.args[1]),1)
            self.assertFalse(tr.session['red'])

    def test_bright_selected_moon_sample_is_red(self):
        work = imread_u(Path(__file__).parent / 'fixtures/brief_red_work.png')
        tr = Tracker(Config())
        tr._anchor = (34, 155, 110, 29)
        tr.session = {'red': False, 'red_blobs': []}
        for _ in range(3):
            tr._scan_red(work)
        self.assertTrue(tr.session['red'], 'selected bright moon sample was missed')

    def test_solar_charger_is_not_red(self):
        panel = imread_u(Path(__file__).parent / 'fixtures/solar_false_positive.png')
        # Saved crop starts at (0,119); OCR label was (37,161,105,21).
        cfg = Config()
        tr = Tracker(cfg)
        tr._anchor = (37, 42, 105, 21)
        tr.session = {'red': False, 'red_blobs': []}
        for _ in range(3):
            tr._scan_red(panel)
        self.assertFalse(tr.session['red'], 'solar charger + red label became a red item')

    def test_disappearing_panel_cannot_confirm_red(self):
        cfg = Config()
        cfg.right_frac = 1.0
        tr = Tracker(cfg)
        tr.in_session = True
        tr._anchor = (10, 10, 60, 20)
        tr.session = {'red': False, 'red_blobs': [], 'observations': 0}
        with patch('track.panel_alive_score', return_value=0.1), \
                patch.object(tr, '_scan_red') as scan:
            tr.step(np.zeros((300, 300, 3), np.uint8))
        scan.assert_not_called()

    def test_item_ocr_uses_panel_coordinates(self):
        cfg = Config()
        cfg.red_confirm_rounds = 1
        tr = Tracker(cfg)
        tr._anchor = (150, 160, 100, 20)
        tr.session = {'red': False, 'red_blobs': []}
        blob = {'bbox': (80, 100, 100, 100), 'accepted': True}
        seen = []
        def check(panel, blobs):
            seen.extend(blobs[0]['bbox'])
            return blobs
        with patch('track.find_red_blobs', return_value={'blobs': [blob]}), \
                patch.object(tr, '_filter_by_text', side_effect=check):
            tr._scan_red(np.zeros((600, 800, 3), np.uint8))
        self.assertEqual(seen, [80, 100, 100, 100])

    def test_different_red_regions_do_not_confirm_each_other(self):
        tr = Tracker(Config())
        tr._anchor = (10, 20, 100, 21)
        tr.session = {'red': False, 'red_blobs': []}
        candidates = iter([
            {'blobs': [{'bbox': (30, 100, 80, 150), 'accepted': True}]},
            {'blobs': [{'bbox': (280, 100, 80, 150), 'accepted': True}]},
        ])
        with patch('track.find_red_blobs', side_effect=lambda *a, **kw: next(candidates)), \
                patch.object(tr, '_filter_by_text', side_effect=lambda panel, b: b):
            for _ in range(2):
                tr._scan_red(np.zeros((500, 800, 3), np.uint8))
        self.assertFalse(tr.session['red'], 'unrelated red regions confirmed each other')

    def test_real_vase_still_confirms(self):
        panel = imread_u(Path(__file__).parent / 'fixtures/run_red_0006.png')
        tr = Tracker(Config())
        tr._anchor = (37, 42, 105, 21)
        tr.session = {'red': False, 'red_blobs': []}
        for _ in range(3):
            tr._scan_red(panel)
        self.assertTrue(tr.session['red'])
        self.assertTrue(tr.session['red_blobs'])


if __name__ == '__main__':
    unittest.main()
