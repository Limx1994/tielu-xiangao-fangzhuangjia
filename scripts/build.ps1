$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
Set-Location $projectRoot
if (-not (Test-Path "tools\ffmpeg\bin\ffmpeg.exe")) { throw "缺少 FFmpeg，请先运行 scripts\fetch_dependencies.ps1" }
if (-not (Test-Path "tools\ocr\ppocr_worker.exe")) { throw "缺少 OCR，请先运行 scripts\fetch_dependencies.ps1" }
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "测试失败，停止打包" }
$shellExe = (Get-Process -Id $PID).Path
& $shellExe -NoProfile -ExecutionPolicy Bypass -File tests\validate_ffmpeg.ps1
if ($LASTEXITCODE -ne 0) { throw "FFmpeg H.264/H.265 验证失败，停止打包" }
$distRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "dist"))
$target = Join-Path $distRoot "XgfzjRecorder"
$token = [guid]::NewGuid().ToString("N")
$stageRoot = Join-Path $distRoot (".xgfzj-stage-" + $token)
$stageTarget = Join-Path $stageRoot "XgfzjRecorder"
$stageWork = Join-Path $stageRoot "work"
$rollback = Join-Path $distRoot (".xgfzj-rollback-" + $token)
$oldConfig = Join-Path $target "config.json"
$configSource = if (Test-Path $oldConfig) { $oldConfig } elseif (Test-Path "config.json") { Join-Path $projectRoot "config.json" } else { $null }

function Assert-DistChild([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $distRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "路径不在发布目录内: $full" }
    return $full
}

function Remove-DistTree([string]$Path) {
    $full = Assert-DistChild $Path
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}

function Move-DistTree([string]$Source, [string]$Destination) {
    $sourceFull = Assert-DistChild $Source
    $destinationFull = Assert-DistChild $Destination
    Move-Item -LiteralPath $sourceFull -Destination $destinationFull
}

New-Item -ItemType Directory -Force -Path $distRoot | Out-Null
try {
    New-Item -ItemType Directory -Path $stageRoot | Out-Null
    & $python -m PyInstaller --noconfirm --clean --distpath $stageRoot --workpath $stageWork embedded_recorder.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
    if (-not (Test-Path -LiteralPath $stageTarget)) { throw "PyInstaller 未生成 staging 发布目录" }
    New-Item -ItemType Directory -Force -Path (Join-Path $stageTarget "tools") | Out-Null
    Copy-Item -LiteralPath "tools\ffmpeg" -Destination (Join-Path $stageTarget "tools\ffmpeg") -Recurse -Force
    $ocrTarget = Join-Path $stageTarget "tools\ocr"
    New-Item -ItemType Directory -Force -Path $ocrTarget | Out-Null
    Get-ChildItem -LiteralPath "tools\ocr" -Force | Where-Object { $_.Name -notin @("ppocr_service.exe", "SHA256SUMS.json") } |
        Copy-Item -Destination $ocrTarget -Recurse -Force
    $checksumPath = Join-Path $ocrTarget "SHA256SUMS.json"
    Remove-Item -LiteralPath (Join-Path $ocrTarget "ppocr_service.exe") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $checksumPath -Force -ErrorAction SilentlyContinue
    $releaseFiles = @(Get-ChildItem -LiteralPath $ocrTarget -Recurse -File | Sort-Object FullName)
    $releaseChecksums = @($releaseFiles | Get-FileHash -Algorithm SHA256 | ForEach-Object {
        [pscustomobject]@{ Path = $_.Path.Substring($ocrTarget.Length + 1); SHA256 = $_.Hash }
    })
    $releaseChecksums | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $checksumPath -Encoding utf8
    $listedChecksums = Get-Content -Raw -LiteralPath $checksumPath | ConvertFrom-Json
    if ($listedChecksums.Count -ne $releaseFiles.Count) { throw "发布目录 OCR 校验清单文件数量不一致" }
    $listedPaths = @{}
    foreach ($entry in $listedChecksums) {
        if ($listedPaths.ContainsKey($entry.Path)) { throw "发布目录 OCR 校验清单包含重复路径: $($entry.Path)" }
        $listedPaths[$entry.Path] = $entry.SHA256
    }
    foreach ($file in $releaseFiles) {
        $relativePath = $file.FullName.Substring($ocrTarget.Length + 1)
        $actualHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        if (-not $listedPaths.ContainsKey($relativePath) -or $listedPaths[$relativePath] -ne $actualHash) {
            throw "发布目录 OCR 校验失败: $relativePath"
        }
    }
    Copy-Item -LiteralPath "README.md" -Destination (Join-Path $stageTarget "README.md") -Force
    Write-Host "发布说明已复制: $(Join-Path $stageTarget 'README.md')"
    if ($configSource) { Copy-Item -LiteralPath $configSource -Destination (Join-Path $stageTarget "config.json") -Force }
    & $python tests\smoke_dist.py $stageTarget
    if ($LASTEXITCODE -ne 0) { throw "打包程序冒烟测试失败" }
    Remove-Item -LiteralPath (Join-Path $stageTarget "runtime\logs\app.log") -Force -ErrorAction SilentlyContinue
    $oldMoved = $false
    try {
        if (Test-Path -LiteralPath $target) { Move-DistTree $target $rollback; $oldMoved = $true }
        Move-DistTree $stageTarget $target
    } catch {
        if ($oldMoved) {
            if (Test-Path -LiteralPath $target) { Remove-DistTree $target }
            if (Test-Path -LiteralPath $rollback) { Move-DistTree $rollback $target }
        }
        throw
    }
    if (Test-Path -LiteralPath $rollback) {
        try { Remove-DistTree $rollback } catch { Write-Warning "新版本已切换，但旧发布目录清理失败: $($_.Exception.Message)" }
    }
    Write-Host "构建完成: $target"
} finally {
    if (Test-Path -LiteralPath $stageRoot) { Remove-DistTree $stageRoot }
}
