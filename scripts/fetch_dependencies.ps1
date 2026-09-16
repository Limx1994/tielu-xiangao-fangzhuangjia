param(
    [string]$OcrCommit = "e234aac4285e6f7bd2541fd237e9b5560fddd0df",
    [switch]$SkipFfmpeg,
    [switch]$UseGit
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ffmpegVersion = "8.1.1"
$ffmpegArchiveHash = "6F58CE889F59C311410F7D2B18895B33C03456463486F3B1EBC93D97A0F54541"
$ffmpegArchiveUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$ffmpegVersion/ffmpeg-$ffmpegVersion-essentials_build.zip"
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolRoot = Join-Path $projectRoot "tools"
New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null

function Get-GithubJson([string]$Uri) {
    $lastError = $null
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        try { return Invoke-RestMethod -Uri $Uri -Headers @{ "User-Agent" = "XgfzjBuilder" } -TimeoutSec 60 }
        catch {
            $lastError = $_.Exception
            if ($attempt -eq 6) { break }
            Start-Sleep -Seconds ([Math]::Min(2 * $attempt, 10))
        }
    }
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    if (-not $curl) { throw $lastError }
    $json = & $curl -L --http1.1 --fail --retry 3 --connect-timeout 30 --max-time 180 --silent --show-error -H "User-Agent: XgfzjBuilder" $Uri
    if ($LASTEXITCODE -ne 0) { throw "GitHub JSON 请求失败: $Uri" }
    try { return ($json | Out-String | ConvertFrom-Json) }
    catch { throw "GitHub JSON 响应无效: $Uri；$($_.Exception.Message)" }
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

function Get-GitBlobSha1([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA1]::Create()
    try {
        $prefix = [Text.Encoding]::ASCII.GetBytes("blob $($stream.Length)`0")
        [void]$hash.TransformBlock($prefix, 0, $prefix.Length, $prefix, 0)
        $buffer = New-Object byte[] 1048576
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            [void]$hash.TransformBlock($buffer, 0, $read, $buffer, 0)
        }
        [void]$hash.TransformFinalBlock([byte[]]@(), 0, 0)
        return ([BitConverter]::ToString($hash.Hash)).Replace("-", "").ToLowerInvariant()
    } finally {
        $hash.Dispose(); $stream.Dispose()
    }
}

function Get-LfsInfo([string]$Commit, [string]$RepositoryPath) {
    $pointer = Join-Path $env:TEMP ("xgfzj-lfs-" + [guid]::NewGuid().ToString("N"))
    try {
        Save-GithubFile "https://raw.githubusercontent.com/Limx1994/PaddleOCR-MinGW-LMX/$Commit/$RepositoryPath" $pointer
        $text = Get-Content -LiteralPath $pointer -Raw
        $oid = [regex]::Match($text, '(?m)^oid sha256:([0-9a-f]{64})\r?$')
        $size = [regex]::Match($text, '(?m)^size ([0-9]+)\r?$')
        if (-not $oid.Success -or -not $size.Success) {
            throw "无法读取 OCR LFS 元数据: $RepositoryPath"
        }
        return @{ SHA256 = $oid.Groups[1].Value; Size = [long]$size.Groups[1].Value }
    } finally {
        Remove-Item -LiteralPath $pointer -Force -ErrorAction SilentlyContinue
    }
}

function Assert-OcrFile([string]$Path, [long]$ExpectedSize, [string]$ExpectedHash, [string]$HashKind) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "OCR 文件缺失: $Path" }
    $actualSize = (Get-Item -LiteralPath $Path).Length
    if ($actualSize -ne $ExpectedSize) { throw "OCR 文件大小不匹配: $Path，期望 $ExpectedSize，实际 $actualSize" }
    $actualHash = if ($HashKind -eq "sha256") { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() } else { Get-GitBlobSha1 $Path }
    if ($actualHash -ne $ExpectedHash.ToLowerInvariant()) { throw "OCR 文件 $HashKind 不匹配: $Path，期望 $ExpectedHash，实际 $actualHash" }
}

if (-not $OcrCommit) {
    try { $OcrCommit = (git ls-remote https://github.com/Limx1994/PaddleOCR-MinGW-LMX.git refs/heads/main).Split()[0] } catch {}
    if (-not $OcrCommit) { $OcrCommit = (Get-GithubJson "https://api.github.com/repos/Limx1994/PaddleOCR-MinGW-LMX/commits/main").sha }
}
if ($OcrCommit -notmatch '^[0-9a-f]{40}$') { throw "OCR commit 无效: $OcrCommit" }
$checkout = Join-Path $env:TEMP ("xgfzj-ocr-" + [guid]::NewGuid().ToString("N"))
$ocrTarget = Join-Path $toolRoot "ocr"
$patchInfoPath = Join-Path $ocrTarget "WORKER_PATCH.json"
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
        $_.path -ne "dist/ppocr/README.md" -and $_.path -ne "dist/ppocr/.json" -and
        $_.path -notmatch '/(test_images|output)/' -and
        $_.path -notmatch '(^|/)(ppocr\.exe|ppocr_client\.exe|ppocr_service\.exe|stress_test\.py|test\.jpg|stdout\.txt|stderr\.txt)$'
    }
    foreach ($item in $files) {
        $relative = $item.path.Substring("dist/ppocr/".Length)
        $destination = Join-Path $ocrTarget ($relative.Replace("/", "\"))
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        $isLfs = $item.size -le 200 -and $item.path -match '\.(exe|dll|pdiparams|onnx)$'
        $lfsInfo = if ($isLfs) { Get-LfsInfo $OcrCommit $item.path } else { $null }
        $expectedSize = if ($isLfs) { $lfsInfo.Size } else { [long]$item.size }
        $expectedHash = if ($isLfs) { $lfsInfo.SHA256 } else { [string]$item.sha }
        $hashKind = if ($isLfs) { "sha256" } else { "git-sha1" }
        if (Test-Path $destination) {
            if ($relative -eq "ppocr_worker.exe" -and (Test-Path $patchInfoPath)) {
                try {
                    $patchInfo = Get-Content -LiteralPath $patchInfoPath -Raw | ConvertFrom-Json
                    $patchedHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
                    if ((Get-Item -LiteralPath $destination).Length -eq $expectedSize -and $patchInfo.upstream_sha256 -eq $expectedHash -and $patchInfo.patched_sha256 -eq $patchedHash) { continue }
                } catch { Write-Warning "OCR worker patch 缓存信息无效：$($_.Exception.Message)" }
            }
            try { Assert-OcrFile $destination $expectedSize $expectedHash $hashKind; continue }
            catch { Write-Warning "OCR 缓存校验失败，重新下载 $relative：$($_.Exception.Message)" }
        }
        if ($isLfs) {
            $mediaUri = "https://media.githubusercontent.com/media/Limx1994/PaddleOCR-MinGW-LMX/$OcrCommit/$($item.path)"
        } else {
            $mediaUri = "https://raw.githubusercontent.com/Limx1994/PaddleOCR-MinGW-LMX/$OcrCommit/$($item.path)"
        }
        Write-Host "下载 $relative"
        Save-GithubFile $mediaUri $destination
        Assert-OcrFile $destination $expectedSize $expectedHash $hashKind
    }
}
$required = @("ppocr_worker.exe", "models\plate_rtdetr.onnx")
foreach ($file in $required) {
    $path = Join-Path $ocrTarget $file
    if (-not (Test-Path $path) -or (Get-Item $path).Length -lt 100000) { throw "OCR 文件缺失或为 LFS 指针: $file" }
}
Remove-Item -LiteralPath (Join-Path $ocrTarget "ppocr_service.exe") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $ocrTarget ".json") -Force -ErrorAction SilentlyContinue
$configDir = Join-Path $ocrTarget "configs"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "ocr\OCR.yaml") -Destination (Join-Path $configDir "OCR.yaml") -Force
$workerPath = Join-Path $ocrTarget "ppocr_worker.exe"
$beforePatch = (Get-FileHash -LiteralPath $workerPath -Algorithm SHA256).Hash
$upstreamHash = if (Test-Path $patchInfoPath) { (Get-Content -Raw $patchInfoPath | ConvertFrom-Json).upstream_sha256 } else { $beforePatch }
$needle = "utility.cc"
$replacement = "./x/y.cc.."
$positions = New-Object System.Collections.Generic.List[long]
$sourcePositions = New-Object System.Collections.Generic.List[long]
$fixedPositions = New-Object System.Collections.Generic.List[long]
$finalPositions = New-Object System.Collections.Generic.List[long]
$readStream = [IO.File]::Open($workerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
try {
    $buffer = New-Object byte[] (4MB)
    $offset = 0L
    while (($count = $readStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $text = [Text.Encoding]::ASCII.GetString($buffer, 0, $count)
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
    $token = [guid]::NewGuid().ToString("N")
    $ffmpegArchive = Join-Path $env:TEMP ("xgfzj-ffmpeg-" + $token + ".zip")
    $ffmpegExtract = Join-Path $env:TEMP ("xgfzj-ffmpeg-" + $token)
    try {
        Save-GithubFile $ffmpegArchiveUrl $ffmpegArchive
        $actualHash = (Get-FileHash -LiteralPath $ffmpegArchive -Algorithm SHA256).Hash
        if ($actualHash -ne $ffmpegArchiveHash) { throw "FFmpeg 压缩包 SHA-256 不匹配: $actualHash" }
        Expand-Archive -LiteralPath $ffmpegArchive -DestinationPath $ffmpegExtract
        $ffmpegSource = Get-ChildItem -LiteralPath $ffmpegExtract -Filter "ffmpeg.exe" -File -Recurse | Select-Object -First 1
        if (-not $ffmpegSource) { throw "FFmpeg 压缩包缺少 ffmpeg.exe" }
        $sourceBin = Split-Path -Parent $ffmpegSource.FullName
        if (-not (Test-Path -LiteralPath (Join-Path $sourceBin "ffprobe.exe"))) { throw "FFmpeg 压缩包缺少 ffprobe.exe" }
        New-Item -ItemType Directory -Force -Path $ffmpegBin | Out-Null
        foreach ($name in @("ffmpeg.exe", "ffprobe.exe")) {
            $source = Join-Path $sourceBin $name
            if ((Get-Item -LiteralPath $source).Length -lt 5000000) { throw "FFmpeg 文件异常: $name" }
            Copy-Item -LiteralPath $source -Destination (Join-Path $ffmpegBin $name) -Force
        }
        $versionLine = (& (Join-Path $ffmpegBin "ffmpeg.exe") -version | Select-Object -First 1)
        if ($LASTEXITCODE -ne 0 -or $versionLine -notmatch "^ffmpeg version $([regex]::Escape($ffmpegVersion))-essentials_build-www\.gyan\.dev") { throw "FFmpeg 版本验证失败: $versionLine" }
        @{
            version = $ffmpegVersion
            archive_url = $ffmpegArchiveUrl
            archive_sha256 = $ffmpegArchiveHash
            ffmpeg_sha256 = (Get-FileHash -LiteralPath (Join-Path $ffmpegBin "ffmpeg.exe") -Algorithm SHA256).Hash
            ffprobe_sha256 = (Get-FileHash -LiteralPath (Join-Path $ffmpegBin "ffprobe.exe") -Algorithm SHA256).Hash
        } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ffmpegRoot "VERSION.json") -Encoding utf8
    } finally {
        Remove-Item -LiteralPath $ffmpegArchive -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $ffmpegExtract) {
            $extractFull = [IO.Path]::GetFullPath($ffmpegExtract)
            $tempPrefix = [IO.Path]::GetFullPath($env:TEMP).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            if (-not $extractFull.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "FFmpeg 临时目录越界: $extractFull" }
            Remove-Item -LiteralPath $extractFull -Recurse -Force
        }
    }
}
Write-Host "依赖准备完成。OCR commit: $OcrCommit"
