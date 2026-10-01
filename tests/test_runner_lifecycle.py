from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
import runner
from detect import Config

class LifecycleTests(unittest.TestCase):
    def test_missing_target_is_saved_as_error_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            r=runner.Runner(Config(),window='missing target')
            with patch.object(runner,'RUNS',Path(directory)),patch.object(runner,'Grabber',side_effect=LookupError('missing target')) as grab:
                self.assertTrue(r.start());r._thread.join(5)
            self.assertFalse(r.status()['running'])
            self.assertFalse(grab.call_args.kwargs['allow_fallback'])
            self.assertEqual(r.store.run(r.run_dir.name)['status'],'error')
            self.assertEqual(r.store.run(r.run_dir.name)['total'],0)

if __name__=='__main__':unittest.main()
