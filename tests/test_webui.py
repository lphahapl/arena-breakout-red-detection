import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from detect import Config
from runner import Runner
from storage import RunStore
from webui import Handler, server_info, _is_our_server, ExclusiveHTTPServer


class HistoryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runner = Runner(Config())
        self.runner._store = RunStore(Path(self.temp.name))
        self.runner.store.update_run('old', {'status': 'stopped'})
        self.runner.store.append_event('old', {'seq': 1, 'kind': 'red', 't': '2026-09-30'})
        class TestHandler(Handler):
            runner = self.runner
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), TestHandler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.temp.cleanup()

    def test_history_survives_with_no_current_run(self):
        with urlopen(self.url + '/history') as response:
            self.assertIn('运行历史', response.read().decode())
        with urlopen(self.url + '/api/history') as response:
            data = json.load(response)
        self.assertEqual(data['runs'][0]['total'], 1)
        self.assertIsNone(self.runner.run_dir)
        with urlopen(self.url + '/api/events?run=old') as response:
            self.assertEqual(json.load(response)['events'][0]['kind'], 'red')
        body = json.dumps({'run': 'old', 'seq': 1, 'label': 'clean'}).encode()
        with urlopen(Request(self.url + '/api/review', body, {'Content-Type': 'application/json'})) as response:
            self.assertTrue(json.load(response)['ok'])
        self.runner._store = RunStore(Path(self.temp.name))
        with urlopen(self.url + '/api/run?run=old') as response:
            self.assertEqual(json.load(response)['false_positive'], 1)

    def test_bad_parameters_and_traversal_are_rejected(self):
        for path in ['/api/events?since=abc', '/api/events?run=..%5Coutside',
                     '/api/history?offset=abc', '/shot/old/..%5Coutside.png']:
            with self.assertRaises(HTTPError) as error:
                urlopen(self.url + path)
            self.assertEqual(error.exception.code, 400)

    def test_server_reuse_requires_same_version_and_storage(self):
        info=server_info(self.runner)
        self.assertTrue(_is_our_server('127.0.0.1',self.server.server_port,info))
        self.assertFalse(_is_our_server('127.0.0.1',self.server.server_port,{**info,'version':'old'}))
        self.assertFalse(_is_our_server('127.0.0.1',self.server.server_port,{**info,'storage_id':'other'}))

    def test_exclusive_port_cannot_be_shared(self):
        with ExclusiveHTTPServer(('127.0.0.1',0),Handler) as server:
            with self.assertRaises(OSError):
                ThreadingHTTPServer(('127.0.0.1',server.server_port),Handler)


if __name__ == '__main__':
    unittest.main()
