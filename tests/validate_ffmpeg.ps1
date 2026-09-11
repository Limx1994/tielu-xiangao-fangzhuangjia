$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$ffmpeg = Join-Path $projectRoot "tools\ffmpeg\bin\ffmpeg.exe"
$ffprobe = Join-Path $projectRoot "tools\ffmpeg\bin\ffprobe.exe"
$validationRoot = Join-Path $projectRoot "runtime\validation"
$resolvedProject = (Resolve-Path $projectRoot).Path
if (Test-Path $validationRoot) {
    $resolvedTarget = (Resolve-Path $validationRoot).Path
    if (-not $resolvedTarget.StartsWith($resolvedProject, [StringComparison]::OrdinalIgnoreCase)) { throw "验证目录越界" }
    Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $validationRoot | Out-Null

foreach ($codec in @("h264", "h265")) {
    $codecDir = Join-Path $validationRoot $codec
    New-Item -ItemType Directory -Force -Path $codecDir | Out-Null
    $encoder = if ($codec -eq "h264") { "libx264" } else { "libx265" }
    $codecArgs = if ($codec -eq "h264") {
        @("-g", "50", "-keyint_min", "50", "-sc_threshold", "0")
    } else {
        @("-x265-params", "keyint=50:min-keyint=50:scenecut=0")
    }
    $segmentPattern = Join-Path $codecDir "segment_%03d.ts"
    & $ffmpeg -hide_banner -loglevel error -f lavfi -i "testsrc2=size=640x360:rate=25" -t 16 -an -c:v $encoder -threads 12 @codecArgs -f segment -segment_time 4 -reset_timestamps 1 -y $segmentPattern
    if ($LASTEXITCODE -ne 0) { throw "$codec 切片生成失败" }
    $segments = Get-ChildItem -LiteralPath $codecDir -Filter "*.ts" | Sort-Object Name
    if ($segments.Count -lt 3) { throw "$codec 切片数量不足: $($segments.Count)" }
    $concat = Join-Path $codecDir "concat.txt"
    $concatLines = $segments | ForEach-Object { "file '$($_.FullName.Replace("'", "'\''"))'" }
    [IO.File]::WriteAllLines($concat, $concatLines, (New-Object Text.UTF8Encoding($false)))
    $output = Join-Path $codecDir "event.mp4"
    & $ffmpeg -hide_banner -loglevel error -f concat -safe 0 -i $concat -map 0:v:0 -an -c copy -movflags +faststart -y $output
    if ($LASTEXITCODE -ne 0) { throw "$codec 无损拼接失败" }
    $duration = [double](& $ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 $output)
    if ($LASTEXITCODE -ne 0 -or $duration -lt 15.5) { throw "$codec 拼接时长异常: $duration" }
    Write-Host "$codec 验证通过：$($segments.Count) 个切片，拼接时长 $duration 秒"
}
& $python -c "from pathlib import Path; import app; a={app.stream_signature(p) for p in Path('runtime/validation/h264').glob('segment_*.ts')}; b={app.stream_signature(p) for p in Path('runtime/validation/h265').glob('segment_*.ts')}; assert len(a)==len(b)==1 and a!=b"
if ($LASTEXITCODE -ne 0) { throw "切片编码参数一致性校验失败" }
