import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from environment import Environment


class PreparationTests(unittest.TestCase):
    def finish(self, env):
        env.start()
        env._thread.join(5)
        self.assertFalse(env._thread.is_alive())
        return env.status()

    def test_installed_does_not_install(self):
        calls = []
        env = Environment(lambda install: calls.append(install) or {'status': 'ready', 'message': 'ok'})
        self.assertEqual(self.finish(env)['status'], 'ready')
        self.assertEqual(calls, [False])

    def test_missing_install_verify(self):
        calls, verified = [], []
        def execute(install):
            calls.append(install)
            if install:
                self.assertEqual(env.status()['status'], 'installing')
            return {'status': 'ready' if install else 'missing', 'message': 'ok'}
        env = Environment(execute, lambda: verified.append(True))
        self.assertEqual(self.finish(env)['status'], 'ready')
        self.assertEqual(calls, [False, True])
        self.assertEqual(verified, [True])

    def test_failure_cancel_and_retry(self):
        answers = iter([{'status':'missing'}, {'status':'error', 'message':'授权取消'}, {'status':'ready', 'message':'ok'}])
        env = Environment(lambda _: next(answers))
        self.assertEqual(self.finish(env)['status'], 'error')
        self.assertEqual(self.finish(env)['status'], 'ready')

    def test_no_duplicate_install_and_verification_failure(self):
        entered, release = threading.Event(), threading.Event()
        def execute(_):
            entered.set(); release.wait(5)
            return {'status':'ready'}
        def verify(): raise RuntimeError('引擎不可用')
        env = Environment(execute, verify)
        env.start(); self.assertTrue(entered.wait(2))
        self.assertFalse(env.start())
        release.set(); env._thread.join(5)
        self.assertEqual(env.status()['status'], 'error')
        self.assertIn('引擎不可用', env.status()['message'])

    def test_blocks_run_until_ready_without_creating_history(self):
        from runner import Runner
        from detect import Config
        runner = Runner(Config())
        runner.environment = Environment()
        self.assertFalse(runner.start())
        self.assertIsNone(runner.run_dir)
        self.assertFalse(runner.status()['running'])

    def test_http_environment_and_retry(self):
        from runner import Runner
        from detect import Config
        from webui import Handler
        runner = Runner(Config())
        runner.environment = Environment(lambda _: {'status':'ready', 'message':'ok'})
        class TestHandler(Handler): pass
        TestHandler.runner = runner
        with ThreadingHTTPServer(('127.0.0.1', 0), TestHandler) as server:
            worker = threading.Thread(target=server.serve_forever); worker.start()
            url = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(url+'/api/environment') as r:
                    self.assertEqual(json.load(r)['status'], 'checking')
                with urlopen(Request(url+'/api/start', data=b'{}')) as r: pass
            except HTTPError as error:
                self.assertIn('error', json.load(error))
            finally:
                with urlopen(Request(url+'/api/environment/retry', data=b'{}')) as r:
                    self.assertTrue(json.load(r)['started'])
                runner.environment._thread.join(5)
                self.assertEqual(runner.environment.status()['status'], 'ready')
                server.shutdown(); worker.join()

if __name__ == '__main__': unittest.main()
