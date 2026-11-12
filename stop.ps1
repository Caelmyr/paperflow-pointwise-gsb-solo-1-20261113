$ErrorActionPreference = 'Stop'
$PidPath = Join-Path $PSScriptRoot '.paperflow.pid'
if (!(Test-Path $PidPath)) { Write-Host '没有启动记录。'; exit 0 }
$SavedPid = [int](Get-Content $PidPath)
$Process = Get-CimInstance Win32_Process -Filter "ProcessId=$SavedPid" -ErrorAction SilentlyContinue
$RunPath = Join-Path $PSScriptRoot 'run.py'
if ($Process -and $Process.CommandLine -and $Process.CommandLine.Contains($RunPath)) {
    Stop-Process -Id $SavedPid
    Write-Host 'PaperFlow 已停止。进行中的批量任务将在下次启动时标记为中断。'
} elseif ($Process) { throw 'PID 已被其他程序使用，未停止该程序。' }
Remove-Item $PidPath -Force
