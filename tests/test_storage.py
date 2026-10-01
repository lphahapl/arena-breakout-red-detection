import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import RunStore


class StorageTests(unittest.TestCase):
    def test_import_restart_review_and_incremental_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / '20260930_214838'
            run.mkdir()
            event = {'seq': 1, 'kind': 'red', 't': '2026-10-01T01:42:49'}
            (run / 'events.jsonl').write_text(json.dumps(event) + '\n{"seq":', encoding='utf-8')
            store = RunStore(root)
            self.assertEqual(store.run(run.name)['total'], 1)
            store.review(run.name, 1, 'clean')
            store = RunStore(root)
            store.import_legacy()
            self.assertEqual(store.events(run.name)['events'][0]['review'], 'clean')
            self.assertEqual(store.run(run.name)['false_positive'], 1)
            store.append_event(run.name, {'seq': 2, 'kind': 'clean'})
            result = store.events(run.name, since=1)
            self.assertEqual([e['seq'] for e in result['events']], [2])
            self.assertEqual(result['last_seq'], 2)
            self.assertEqual(RunStore(root).run(run.name)['total'], 2)

    def test_empty_run_and_interrupted_run_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            store.update_run('empty', {'status': 'running', 'started_at': '2026-10-01T00:00:00'})
            store = RunStore(Path(directory))
            self.assertEqual(store.run('empty')['status'], 'interrupted')
            self.assertEqual(store.run('empty')['total'], 0)
            self.assertIsNone(store.run('missing'))

    def test_parallel_writes_keep_every_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            workers = [threading.Thread(target=store.append_event,
                       args=('run', {'seq': i, 'kind': 'clean'})) for i in range(1, 21)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(store.run('run')['total'], 20)

    def test_invalid_paths_and_labels_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            for name in ['../outside', '..\\outside', 'C:\\outside', '', 'a/b']:
                with self.assertRaises(ValueError):
                    store.events(name)
            with self.assertRaises(ValueError):
                store.review('run', 1, 'bad_label')


if __name__ == '__main__':
    unittest.main()
