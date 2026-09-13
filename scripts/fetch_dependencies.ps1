param(
    [string]$OcrCommit = "e234aac4285e6f7bd2541fd237e9b5560fddd0df",
    [switch]$SkipFfmpeg,
    [switch]$UseGit
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolRoot = Join-Path $projectRoot "tools"
New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null

function Get-GithubJson([string]$Uri) {
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        try { return Invoke-RestMethod -Uri $Uri -Headers @{ "User-Agent" = "XgfzjBuilder" } -TimeoutSec 60 }
        catch {
            if ($attempt -eq 6) { throw }
            Start-Sleep -Seconds ([Math]::Min(2 * $attempt, 10))
        }
    }
}

function Save-GithubFile([string]$Uri, [string]$Destination) {
    $partial = "$Destination.part"
    Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    if ($curl) {
        & $curl -L --http1.1 --fail --retry 8 --retry-all-errors --retry-delay 3 --connect-timeout 30 --max-time 3600 --silent --show-error -o $partial $Uri
        if ($LASTEXITCODE -ne 0) { throw "下载失败: $Uri" }
        Move-Item -LiteralPath $partial -Destination $Destination -Force
        return
    }
    Invoke-WebRequest -Uri $Uri -Headers @{ "User-Agent" = "XgfzjBuilder" } -OutFile $partial -UseBasicParsing -TimeoutSec 1800
    Move-Item -LiteralPath $partial -Destination $Destination -Force
}

if (-not $OcrCommit) {
    try { $OcrCommit = (git ls-remote https://github.com/Limx1994/PaddleOCR-MinGW-LMX.git refs/heads/main).Split()[0] } catch {}
    if (-not $OcrCommit) { $OcrCommit = (Get-GithubJson "https://api.github.com/repos/Limx1994/PaddleOCR-MinGW-LMX/commits/main").sha }
}
if ($OcrCommit -notmatch '^[0-9a-f]{40}$') { throw "OCR commit 无效: $OcrCommit" }
$checkout = Join-Path $env:TEMP ("xgfzj-ocr-" + [guid]::NewGuid().ToString("N"))
$ocrTarget = Join-Path $toolRoot "ocr"
New-Item -ItemType Directory -Force -Path $ocrTarget | Out-Null
$gitReady = $false
if ($UseGit) { try {
    git clone --filter=blob:none --no-checkout https://github.com/Limx1994/PaddleOCR-MinGW-LMX.git $checkout
    if ($LASTEXITCODE -eq 0) {
        git -C $checkout sparse-checkout init --cone
        git -C $checkout sparse-checkout set dist/ppocr
        git -C $checkout checkout $OcrCommit
        if ($LASTEXITCODE -eq 0) {
            git -C $checkout lfs pull --include="dist/ppocr/**" --exclude="dist/ppocr/ppocr_service.exe"
            $gitReady = $LASTEXITCODE -eq 0
            if ($gitReady) { Copy-Item -Path (Join-Path $checkout "dist\ppocr\*") -Destination $ocrTarget -Recurse -Force }
        }
    }
} finally {
    if (Test-Path $checkout) {
        $resolved = (Resolve-Path $checkout).Path
        $tempResolved = (Resolve-Path $env:TEMP).Path
        if ($resolved.StartsWith($tempResolved, [StringComparison]::OrdinalIgnoreCase)) { Remove-Item -LiteralPath $resolved -Recurse -Force }
    }
} }
if (-not $gitReady) {
    Write-Warning "Git 连接不可用，改用 GitHub media 端点下载固定 commit"
    $treeUri = "https://api.github.com/repos/Limx1994/PaddleOCR-MinGW-LMX/git/trees/$OcrCommit`?recursive=1"
    $files = (Get-GithubJson $treeUri).tree | Where-Object {
        $_.type -eq "blob" -and $_.path.StartsWith("dist/ppocr/") -and
        $_.path -notmatch '/(test_images|output)/' -and
        $_.path -notmatch '(^|/)(ppocr\.exe|ppocr_client\.exe|ppocr_service\.exe|stress_test\.py|test\.jpg|stdout\.txt|stderr\.txt)$'
    }
    foreach ($item in $files) {
        $relative = $item.path.Substring("dist/ppocr/".Length)
        $destination = Join-Path $ocrTarget ($relative.Replace("/", "\"))
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        $isLfs = $item.size -le 200 -and $item.path -match '\.(exe|dll|pdiparams|onnx)$'
        if (Test-Path $destination) {
            $existingSize = (Get-Item $destination).Length
            if (($isLfs -and $existingSize -gt 200) -or (-not $isLfs -and $existingSize -eq $item.size)) { continue }
        }
        if ($isLfs) {
            $mediaUri = "https://media.githubusercontent.com/media/Limx1994/PaddleOCR-MinGW-LMX/$OcrCommit/$($item.path)"
        } else {
            $mediaUri = "https://raw.githubusercontent.com/Limx1994/PaddleOCR-MinGW-LMX/$OcrCommit/$($item.path)"
        }
        Write-Host "下载 $relative"
        Save-GithubFile $mediaUri $destination
    }
}
$required = @("ppocr_worker.exe", "models\plate_rtdetr.onnx")
foreach ($file in $required) {
    $path = Join-Path $ocrTarget $file
    if (-not (Test-Path $path) -or (Get-Item $path).Length -lt 100000) { throw "OCR 文件缺失或为 LFS 指针: $file" }
}
Remove-Item -LiteralPath (Join-Path $ocrTarget "ppocr_service.exe") -Force -ErrorAction SilentlyContinue
$configDir = Join-Path $ocrTarget "configs"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "ocr\OCR.yaml") -Destination (Join-Path $configDir "OCR.yaml") -Force
$workerPath = Join-Path $ocrTarget "ppocr_worker.exe"
$beforePatch = (Get-FileHash -LiteralPath $workerPath -Algorithm SHA256).Hash
$patchInfoPath = Join-Path $ocrTarget "WORKER_PATCH.json"
$upstreamHash = if (Test-Path $patchInfoPath) { (Get-Content -Raw $patchInfoPath | ConvertFrom-Json).upstream_sha256 } else { $beforePatch }
$needle = "utility.cc"
$replacement = "./x/y.cc.."
$positions = New-Object System.Collections.Generic.List[long]
$sourcePositions = New-Object System.Collections.Generic.List[long]
$fixedPositions = New-Object System.Collections.Generic.List[long]
$finalPositions = New-Object System.Collections.Generic.List[long]
$patchedFound = $false
$readStream = [IO.File]::Open($workerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
try {
    $buffer = New-Object byte[] (4MB)
    $offset = 0L
    while (($count = $readStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $text = [Text.Encoding]::ASCII.GetString($buffer, 0, $count)
        if ($text.Contains($replacement)) { $patchedFound = $true }
        $index = $text.IndexOf($needle, [StringComparison]::Ordinal)
        while ($index -ge 0) {
            $positions.Add($offset + $index)
            $index = $text.IndexOf($needle, $index + $needle.Length, [StringComparison]::Ordinal)
        }
        foreach ($pair in @(@("D:\tmp\t", $sourcePositions), @("./x/y.cc", $fixedPositions), @("./x/yyyy", $finalPositions))) {
            $index = $text.IndexOf($pair[0], [StringComparison]::Ordinal)
            while ($index -ge 0) {
                $pair[1].Add($offset + $index)
                $index = $text.IndexOf($pair[0], $index + 8, [StringComparison]::Ordinal)
            }
        }
        $offset += $count
    }
} finally { $readStream.Dispose() }
if ($positions.Count -gt 0) {
    $writeStream = [IO.File]::Open($workerPath, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($replacement)
        foreach ($position in $positions) { $writeStream.Position = $position; $writeStream.Write($bytes, 0, $bytes.Length) }
    } finally { $writeStream.Dispose() }
}
$machineTargets = New-Object System.Collections.Generic.List[long]
$machineFound = $false
$readStream = [IO.File]::Open($workerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
try {
    $checks = @(
        [pscustomobject]@{ Positions = $sourcePositions; LengthByte = 0x3A; IsSource = $true },
        [pscustomobject]@{ Positions = $fixedPositions; LengthByte = 0x08; IsSource = $true },
        [pscustomobject]@{ Positions = $finalPositions; LengthByte = 0x3A; IsSource = $false }
    )
    foreach ($entry in $checks) {
        foreach ($position in $entry.Positions) {
            if ($position -lt 7) { continue }
            $readStream.Position = $position - 7
            $window = New-Object byte[] 36
            if ($readStream.Read($window, 0, $window.Length) -ne $window.Length) { continue }
            $signature = $window[0] -eq 0xBD -and $window[1] -eq 0x02 -and $window[5] -eq 0x48 -and $window[6] -eq 0xBB -and
                $window[15] -eq 0x48 -and $window[16] -eq 0x8D -and $window[17] -eq 0x7C -and $window[18] -eq 0x24 -and
                $window[26] -eq 0x48 -and $window[27] -eq 0xC7 -and $window[34] -eq $entry.LengthByte
            if ($signature) {
                if ($entry.IsSource) { $machineTargets.Add($position) } else { $machineFound = $true }
            }
        }
    }
} finally { $readStream.Dispose() }
if ($machineTargets.Count -gt 0) {
    $writeStream = [IO.File]::Open($workerPath, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $firstBytes = [Text.Encoding]::ASCII.GetBytes("./x/yyyy")
        $eightY = [Text.Encoding]::ASCII.GetBytes("yyyyyyyy")
        $twoY = [Text.Encoding]::ASCII.GetBytes("yy")
        foreach ($position in $machineTargets) {
            $writeStream.Position = $position; $writeStream.Write($firstBytes, 0, $firstBytes.Length)
            foreach ($relative in @(93, 107, 121, 135, 149, 163)) {
                $writeStream.Position = $position + $relative; $writeStream.Write($eightY, 0, $eightY.Length)
            }
            $writeStream.Position = $position + 84; $writeStream.Write($twoY, 0, $twoY.Length)
            $writeStream.Position = $position + 27; $writeStream.WriteByte(0x3A)
        }
    } finally { $writeStream.Dispose() }
    $machineFound = $true
}
if (-not $machineFound) { throw "未找到预期的 OCR worker 配置路径代码，拒绝继续" }
$afterPatch = (Get-FileHash -LiteralPath $workerPath -Algorithm SHA256).Hash
@{ upstream_sha256 = $upstreamHash; patched_sha256 = $afterPatch; string_replacements = $positions.Count; code_replacements = $machineTargets.Count; reason = "修复上游 worker 默认 OCR.yaml 相对路径" } |
    ConvertTo-Json | Set-Content -LiteralPath $patchInfoPath -Encoding utf8
Set-Content -LiteralPath (Join-Path $ocrTarget "UPSTREAM_COMMIT.txt") -Value $OcrCommit -Encoding ascii
Get-ChildItem -Path $ocrTarget -Recurse -File | Where-Object { $_.Name -ne "SHA256SUMS.json" } | Get-FileHash -Algorithm SHA256 | ForEach-Object {
    [pscustomobject]@{ Path = $_.Path.Substring($ocrTarget.Length + 1); SHA256 = $_.Hash }
} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $ocrTarget "SHA256SUMS.json") -Encoding utf8

if (-not $SkipFfmpeg) {
    $ffmpegRoot = Join-Path $toolRoot "ffmpeg"
    $ffmpegBin = Join-Path $ffmpegRoot "bin"
    New-Item -ItemType Directory -Force -Path $ffmpegBin | Out-Null
    foreach ($name in @("ffmpeg.exe", "ffprobe.exe")) {
        $source = (Get-Command $name -ErrorAction Stop).Source
        $candidates = @($source, (Join-Path "C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin" $name))
        $actual = $candidates | Where-Object { (Test-Path $_) -and (Get-Item $_).Length -gt 5000000 } | Select-Object -First 1
        if (-not $actual) { throw "$name 不是可独立复制的静态程序，请安装 FFmpeg essentials static build" }
        Copy-Item -LiteralPath $actual -Destination (Join-Path $ffmpegBin $name) -Force
    }
}
Write-Host "依赖准备完成。OCR commit: $OcrCommit"
