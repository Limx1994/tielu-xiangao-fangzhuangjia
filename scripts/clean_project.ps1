#requires -Version 5.1
<#
.SYNOPSIS
将项目缓存、测试残留和空目录移到 Windows 回收站。
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\clean_project.ps1 -WhatIf
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\clean_project.ps1
.NOTES
请先停止项目服务、测试和构建。保留配置、日志、录像、dist、tools 和 .venv。
移动到回收站不会释放磁盘空间；脚本不会清空回收站。
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$projectPrefix = $projectRoot.TrimEnd('\') + '\'
foreach ($name in @('app.py', 'requirements.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $name) -PathType Leaf)) {
        throw "项目标识文件不存在，停止清理: $name"
    }
}

function Assert-CleanPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "清理路径越界: $full"
    }
    $current = $full
    while ($current -ne $projectRoot) {
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "清理路径或父目录是链接，停止清理: $current"
        }
        $current = Split-Path -Parent $current
    }
    return $full
}

function Get-CleanStats([string]$Path) {
    $prefix = $Path.TrimEnd('\') + '\'
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Path)
    $files = 0
    $bytes = 0L
    while ($pending.Count -gt 0) {
        foreach ($item in Get-ChildItem -LiteralPath ($pending.Pop()) -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                # pytest 的 current 链接必须指向本次清理目录内，不遍历链接。
                if ($item.LinkType -ne 'SymbolicLink' -or -not $item.Target) {
                    throw "无法确认链接目标，停止清理: $($item.FullName)"
                }
                foreach ($target in @($item.Target)) {
                    if (-not [IO.Path]::IsPathRooted($target)) {
                        $target = Join-Path (Split-Path -Parent $item.FullName) $target
                    }
                    $linkPath = [IO.Path]::GetFullPath($target)
                    if (-not $linkPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                        throw "链接指向清理目录之外，停止清理: $($item.FullName)"
                    }
                }
            } elseif ($item.PSIsContainer) {
                $pending.Push($item.FullName)
            } else {
                $files++
                $bytes += $item.Length
            }
        }
    }
    return [pscustomobject]@{ Files = $files; Bytes = $bytes }
}

$tracked = @()
if (Test-Path -LiteralPath (Join-Path $projectRoot '.git')) {
    Get-Command git -ErrorAction Stop | Out-Null
    $tracked = @(git -C $projectRoot ls-files --cached --full-name)
    if ($LASTEXITCODE -ne 0) { throw "读取 Git 文件列表失败，停止清理" }
}

$names = @('__pycache__', '.pytest_cache', '.ruff_cache', 'tests\__pycache__', 'scripts\__pycache__', 'runtime\validation')
$runtimePath = Join-Path $projectRoot 'runtime'
if (Test-Path -LiteralPath $runtimePath) {
    Assert-CleanPath $runtimePath | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $runtimePath -Directory -Force) {
        if ($item.Name -match '^review-(fix-full|fix-target|pytest)-[0-9a-f]{32}$') {
            $names += 'runtime\' + $item.Name
        }
    }
}
$emptyNames = @('configs', 'runtime\cache\test')
$names += $emptyNames
$targets = @()
$skipped = 0
foreach ($name in $names) {
    $path = Join-Path $projectRoot $name
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host "跳过（不存在）: $name"
        $skipped++
        continue
    }
    $path = Assert-CleanPath $path
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "预期目录却发现文件: $path" }
    if ($name -in $emptyNames -and @(Get-ChildItem -LiteralPath $path -Force).Count -gt 0) {
        Write-Host "跳过（目录非空）: $name"
        $skipped++
        continue
    }
    $prefix = $path.TrimEnd('\') + '\'
    foreach ($file in $tracked) {
        $full = [IO.Path]::GetFullPath((Join-Path $projectRoot $file))
        if ($full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or $full -eq $path) {
            throw "清理目录包含 Git 跟踪文件，停止清理: $file"
        }
    }
    $stat = Get-CleanStats $path
    $targets += [pscustomobject]@{ Path = $path; Name = $name; Files = $stat.Files; Bytes = $stat.Bytes }
}

Add-Type -AssemblyName Microsoft.VisualBasic
$moved = 0
$files = 0
$bytes = 0L
foreach ($target in $targets) {
    if ($PSCmdlet.ShouldProcess($target.Path, '移到 Windows 回收站')) {
        Assert-CleanPath $target.Path | Out-Null
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
            $target.Path,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
            [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException
        )
        if (Test-Path -LiteralPath $target.Path) { throw "移动后原路径仍然存在: $($target.Path)" }
        $moved++
        $files += $target.Files
        $bytes += $target.Bytes
        Write-Host "已移到回收站: $($target.Name)"
    } else {
        Write-Host "跳过（预览或未确认）: $($target.Name)，$($target.Files) 个文件，$($target.Bytes) 字节"
        $skipped++
    }
}
Write-Host ("清理完成：移到回收站 {0} 个目录、{1} 个文件、{2:N2} MiB；跳过 {3} 个目录。" -f $moved, $files, ($bytes / 1MB), $skipped)
