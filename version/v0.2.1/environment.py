"""Prepare system OCR without blocking the HTTP server or elevating the app."""
import base64
import json
from pathlib import Path
import subprocess
import threading
import os


def invoke(install: bool) -> dict:
    script = (Path(__file__).with_name('environment.ps1')).read_text(encoding='utf-8-sig')
    source = '$Install=$' + ('true' if install else 'false') + ';\n' + script
    encoded = base64.b64encode(source.encode('utf-16-le')).decode('ascii')
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            capture_output=True, encoding='utf-8-sig', creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Windows PowerShell 执行失败')
    return json.loads(result.stdout.strip())


class Environment:
    def __init__(self, execute=invoke, verify=None):
        self._execute = execute
        self._verify = verify
        self._lock = threading.Lock()
        self._state = {'status': 'checking', 'message': '正在检测中文 OCR 环境…'}
        self._thread = None

    def status(self):
        with self._lock:
            return dict(self._state)

    def _set(self, state):
        with self._lock:
            self._state = state

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._state = {'status': 'checking', 'message': '正在检测中文 OCR 环境…'}
            self._thread = threading.Thread(target=self._prepare, daemon=True)
            self._thread.start()
            return True

    def _prepare(self):
        try:
            result = self._execute(False)
            if result.get('status') == 'missing':
                self._set({'status': 'installing', 'message': '正在安装简体中文 OCR。请允许 Windows 管理员授权，并保持联网；可能需要几分钟。'})
                result = self._execute(True)
            if result.get('status') == 'ready' and self._verify:
                self._verify()
            if result.get('status') not in ('ready', 'error'):
                raise RuntimeError('安装未完成，中文 OCR 仍不可用')
            self._set(result)
        except Exception as error:
            self._set({'status': 'error', 'message': f'环境检测或安装失败：{error}'})


def verify_python():
    import ocr
    # Use the application's actual WinRT projection, in this worker thread.
    engine = ocr.OcrEngine.try_create_from_language(ocr.Language(ocr.DEFAULT_LANG))
    if engine is None:
        raise RuntimeError('Python 中文 OCR 引擎不可用')
