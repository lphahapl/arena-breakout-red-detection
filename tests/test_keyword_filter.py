"""Editable JSON rules, actual OCR filtering and safe per-run reload."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np

from detect import Config
from red_filter import load_exclude_words
from runner import Runner
from track import Tracker


class KeywordFilterTests(unittest.TestCase):
    def test_json_validation_and_editing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'red_filter.json'
            for data, want in [({'exclude_words': ['自定词', '自定词']}, ('自定词',)),
                               ({'exclude_words': []}, ())]:
                path.write_text('\ufeff' + json.dumps(data), encoding='utf-8')
                self.assertEqual(load_exclude_words(path), want)
            for data in [{}, {'exclude_words': '实验室'}, {'exclude_words': ['']},
                         {'exclude_words': [' ']}, {'exclude_words': [1]},
                         {'exclude_words': [], 'typo': True}]:
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_exclude_words(path)
            path.write_text('{', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_exclude_words(path)
            path.unlink()
            with self.assertRaises(ValueError):
                load_exclude_words(path)

    def test_navigation_survives_the_actual_text_filter(self):
        for text, accepted in [('航天导航仪', True), ('航天实验室', False),
                               ('行星之子', False), ('航天实验', True)]:
            tr = Tracker(Config())
            blob = {'bbox': (20, 20, 80, 80), 'accepted': True}
            with patch('track.ocr.recognize', return_value=[{'text': text}]):
                result = tr._filter_by_text(np.zeros((150, 150, 3), np.uint8), [blob])
            self.assertEqual(bool(result), accepted, text)
            self.assertEqual(blob['accepted'], accepted, text)

    def test_custom_words_control_filter(self):
        tr = Tracker(Config(red_exclude_words=('定制词',)))
        with patch('track.ocr.recognize', return_value=[{'text': '定制词'}]):
            self.assertFalse(tr._filter_by_text(np.zeros((150, 150, 3), np.uint8),
                [{'bbox': (20, 20, 80, 80), 'accepted': True}]))

    def test_each_start_reloads_and_invalid_config_creates_no_run(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            rules = base / 'red_filter.json'
            runs = base / 'runs'
            runner = Runner(Config())
            with patch('red_filter.FILTER_PATH', rules), patch('runner.RUNS', runs), \
                    patch.object(runner, '_loop'):
                for words in [['实验室'], []]:
                    rules.write_text(json.dumps({'exclude_words': words}), encoding='utf-8')
                    self.assertTrue(runner.start())
                    runner._thread.join()
                    self.assertEqual(runner.cfg.red_exclude_words, tuple(words))
                    self.assertEqual(runner.store.run(runner.run_dir.name)['config']['red_exclude_words'], words)
                before = list(runs.iterdir())
                rules.write_text('{', encoding='utf-8')
                self.assertFalse(runner.start())
                self.assertEqual(list(runs.iterdir()), before)
                self.assertIn('red_filter.json', runner.status()['error'])


if __name__ == '__main__':
    unittest.main()
