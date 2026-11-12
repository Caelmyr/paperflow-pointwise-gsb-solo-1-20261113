param([int]$Port = 8888)
$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot
$Python = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$Ready = Join-Path $ProjectRoot '.venv/.paperflow-ready'
if (!(Test-Path $Python) -or !(Test-Path $Ready)) {
    $SystemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($SystemPython) {
        try { & $SystemPython.Source -c "import sys; assert sys.version_info >= (3, 10)"; if ($LASTEXITCODE -ne 0) { $SystemPython = $null } } catch { $SystemPython = $null }
    }
    if (!$SystemPython) {
        $Winget = Get-Command winget -ErrorAction SilentlyContinue
        if (!$Winget) { throw '需要 Python 3.10+，当前也未找到 winget，无法自动安装。请安装 Python 或 Docker Desktop。' }
        & winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw 'Python 安装失败' }
        $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
        $SystemPython = Get-Command python -ErrorAction SilentlyContinue
        if (!$SystemPython) { throw 'Python 已安装，请重新打开启动脚本。' }
    }
    & $SystemPython.Source scripts/bootstrap.py
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请查看上方错误。' }
    New-Item $Ready -ItemType File -Force | Out-Null
}
$PidPath = Join-Path $ProjectRoot '.paperflow.pid'
if (Test-Path $PidPath) {
    $SavedPid = [int](Get-Content $PidPath)
    $ExistingProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$SavedPid" -ErrorAction SilentlyContinue
    if ($ExistingProcess -and $ExistingProcess.CommandLine.Contains((Join-Path $ProjectRoot 'run.py'))) { throw 'PaperFlow 已运行，请先使用 stop.bat 停止。' }
    Remove-Item $PidPath -Force
}
$Listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($Listener) { throw "端口 $Port 已占用。请运行 start.bat 8889 或 start.ps1 -Port 8889。" }
$Process = Start-Process -FilePath $Python -ArgumentList @('"' + (Join-Path $ProjectRoot 'run.py') + '"','--port',"$Port") -WorkingDirectory $ProjectRoot -PassThru -RedirectStandardOutput (Join-Path $ProjectRoot 'server.log') -RedirectStandardError (Join-Path $ProjectRoot 'server-error.log')
$Process.Id | Set-Content $PidPath
$Started = $false
for ($Try = 0; $Try -lt 45; $Try++) {
    Start-Sleep -Seconds 1
    if ($Process.HasExited) { throw '服务启动失败，请查看 server-error.log。' }
    try { $Response = Invoke-RestMethod "http://127.0.0.1:$Port/api/health"; if ($Response.status -eq 'ok') { $Started = $true; break } } catch { }
}
if (!$Started) { throw '服务尚未就绪，请查看 server-error.log。' }
Write-Host "PaperFlow 已启动：http://127.0.0.1:$Port"
Start-Process "http://127.0.0.1:$Port"
