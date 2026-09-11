$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
Set-Location $projectRoot
if (-not (Test-Path "tools\ffmpeg\bin\ffmpeg.exe")) { throw "缺少 FFmpeg，请先运行 scripts\fetch_dependencies.ps1" }
if (-not (Test-Path "tools\ocr\ppocr_service.exe")) { throw "缺少 OCR，请先运行 scripts\fetch_dependencies.ps1" }
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "测试失败，停止打包" }
$shellExe = (Get-Process -Id $PID).Path
& $shellExe -NoProfile -ExecutionPolicy Bypass -File tests\validate_ffmpeg.ps1
if ($LASTEXITCODE -ne 0) { throw "FFmpeg H.264/H.265 验证失败，停止打包" }
$target = Join-Path $projectRoot "dist\XgfzjRecorder"
$backup = Join-Path $env:TEMP ("xgfzj-config-" + [guid]::NewGuid().ToString("N") + ".json")
$oldConfig = Join-Path $target "config.json"
if (Test-Path $oldConfig) { Copy-Item -LiteralPath $oldConfig -Destination $backup } elseif (Test-Path "config.json") { Copy-Item -LiteralPath "config.json" -Destination $backup }
try {
    & $python -m PyInstaller --noconfirm --clean embedded_recorder.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
    New-Item -ItemType Directory -Force -Path (Join-Path $target "tools") | Out-Null
    Copy-Item -LiteralPath "tools\ffmpeg" -Destination (Join-Path $target "tools\ffmpeg") -Recurse -Force
    Copy-Item -LiteralPath "tools\ocr" -Destination (Join-Path $target "tools\ocr") -Recurse -Force
    Copy-Item -LiteralPath "README.md" -Destination (Join-Path $target "README.md") -Force
    Write-Host "发布说明已复制: $(Join-Path $target 'README.md')"
    if (Test-Path $backup) { Copy-Item -LiteralPath $backup -Destination (Join-Path $target "config.json") -Force }
    & $python tests\smoke_dist.py
    if ($LASTEXITCODE -ne 0) { throw "打包程序冒烟测试失败" }
    Remove-Item -LiteralPath (Join-Path $target "runtime\logs\app.log") -Force -ErrorAction SilentlyContinue
    Write-Host "构建完成: $target"
} finally { Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue }
