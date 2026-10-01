# Shared by the Python launcher and embedded Rust executable. No policy changes.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function Test-ChineseOcr {
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
    $language = [Windows.Globalization.Language]::new('zh-Hans-CN')
    return $null -ne [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
}
try {
    if (Test-ChineseOcr) {
        @{status='ready'; message='简体中文 OCR 已就绪'} | ConvertTo-Json -Compress
        exit 0
    }
    if (-not $Install) {
        @{status='missing'; message='缺少简体中文 OCR'} | ConvertTo-Json -Compress
        exit 0
    }
    $logPath = Join-Path ([IO.Path]::GetTempPath()) ('ab-ocr-' + [Guid]::NewGuid().ToString('N') + '.log')
    # The elevated command contains only fixed Windows capability operations and a log path.
    $installer = @'
$ErrorActionPreference = 'Stop'
try {
    foreach ($name in @('Language.Basic~~~zh-CN~0.0.1.0', 'Language.OCR~~~zh-CN~0.0.1.0')) {
        $cap = Get-WindowsCapability -Online -Name $name
        if ($cap.State -ne 'Installed') {
            $result = Add-WindowsCapability -Online -Name $name
            if ($result.RestartNeeded) { throw '安装完成后需要重启 Windows，再打开检测程序。' }
        }
    }
    exit 0
} catch {
    $_.Exception.Message | Set-Content -LiteralPath '__LOG__' -Encoding UTF8
    exit 1
}
'@
    $installer = $installer.Replace('__LOG__', $logPath.Replace("'", "''"))
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($installer))
    # Wait in the unprivileged process. Installation can take several minutes.
    $process = Start-Process -FilePath "$PSHOME\powershell.exe" -Verb RunAs -WindowStyle Hidden -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        $detail = if (Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath -Raw -Encoding UTF8 } else { '安装未完成，请检查联网和 Windows Update。' }
        throw $detail
    }
    if (-not (Test-ChineseOcr)) { throw '安装后 OCR 仍不可用。请重启 Windows 后重试。' }
    @{status='ready'; message='简体中文 OCR 安装完成并验证可用'} | ConvertTo-Json -Compress
} catch {
    @{status='error'; message=('中文 OCR 环境准备失败：' + $_.Exception.Message + ' 如取消了管理员授权，可点击重新检测并安装。')} | ConvertTo-Json -Compress
} finally {
    if ($logPath -and (Test-Path -LiteralPath $logPath)) { Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue }
}
