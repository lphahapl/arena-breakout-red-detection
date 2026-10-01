"""Execute the shipped PS workflow with mocked OS operations; never remove/install OS features."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = (Path(__file__).resolve().parents[1]/'environment.ps1').read_text(encoding='utf-8')
SHELL = str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')

def run_mock(mode, install=True):
    with tempfile.TemporaryDirectory() as directory:
        operations = str(Path(directory)/'operations.log').replace("'", "''")
        # Overrides are inserted after the production probe definition. The remaining
        # flow, encoded elevated installer, capability names, errors and recheck run unchanged.
        mock = '''
$script:probeCount = 0
function Test-ChineseOcr {
    $script:probeCount++
    return ('__MODE__' -eq 'installed') -or (($script:probeCount -gt 1) -and ('__MODE__' -ne 'still-missing'))
}
function Start-Process {
    param($FilePath, $Verb, $WindowStyle, $ArgumentList, [switch]$Wait, [switch]$PassThru)
    if ($Verb -ne 'RunAs' -or $WindowStyle -ne 'Hidden' -or -not $Wait) { throw 'incorrect installation launch' }
    if ('__MODE__' -eq 'cancel') { throw '管理员授权已取消' }
    $body = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($ArgumentList[-1]))
    $fakeOs = @'
function Get-WindowsCapability {
    param([switch]$Online, $Name)
    return [PSCustomObject]@{ State= $(if ('__MODE__' -eq 'basic-installed' -and $Name -like 'Language.Basic*') {'Installed'} else {'NotPresent'}) }
}
function Add-WindowsCapability {
    param([switch]$Online, $Name)
    $Name | Add-Content -LiteralPath '__OPERATIONS__'
    if ('__MODE__' -eq 'failed') { throw 'Windows Update 不可用' }
    return [PSCustomObject]@{RestartNeeded=('__MODE__' -eq 'restart')}
}
'@
    $childEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($fakeOs + "`n" + $body))
    & "$PSHOME\\powershell.exe" -NoProfile -NonInteractive -EncodedCommand $childEncoded
    return [PSCustomObject]@{ExitCode=$LASTEXITCODE}
}
'''.replace('__MODE__', mode).replace('__OPERATIONS__', operations)
        text = '$Install=$' + ('true' if install else 'false') + ';\n' + SCRIPT.replace('\ntry {\n    if (Test-ChineseOcr)', mock+'\ntry {\n    if (Test-ChineseOcr)', 1)
        assert mock in text
        encoded = base64.b64encode(text.encode('utf-16-le')).decode()
        result = subprocess.run([SHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],capture_output=True,encoding='utf-8-sig',creationflags=0x08000000,timeout=30)
        if result.returncode: raise RuntimeError(result.stderr)
        return json.loads(result.stdout.strip()), Path(operations).read_text().splitlines() if Path(operations).exists() else []

class PowerShellWorkflowTests(unittest.TestCase):
    def test_existing_does_not_elevate(self):
        result, operations = run_mock('installed')
        self.assertEqual(result['status'], 'ready'); self.assertEqual(operations, [])

    def test_check_only_never_installs(self):
        result, operations = run_mock('missing', False)
        self.assertEqual(result['status'], 'missing'); self.assertEqual(operations, [])

    def test_install_basic_then_ocr_and_skip_present(self):
        result, operations = run_mock('missing')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(operations, ['Language.Basic~~~zh-CN~0.0.1.0', 'Language.OCR~~~zh-CN~0.0.1.0'])
        result, operations = run_mock('basic-installed')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(operations, ['Language.OCR~~~zh-CN~0.0.1.0'])

    def test_cancel_failure_restart_and_failed_recheck(self):
        for mode, message in [('cancel','授权已取消'), ('failed','Windows Update'), ('restart','重启 Windows'), ('still-missing','仍不可用')]:
            with self.subTest(mode=mode):
                result, _ = run_mock(mode)
                self.assertEqual(result['status'], 'error')
                self.assertIn(message, result['message'])

if __name__ == '__main__': unittest.main()
