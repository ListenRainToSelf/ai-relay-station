<#
.SYNOPSIS
  本地 AI 中转站 · Windows 安装/管理脚本

.DESCRIPTION
  创建虚拟环境、安装依赖、生成启动快捷方式，并可选写入注册表实现开机自启。
  日常使用可直接双击项目根目录的「启动中转站.cmd」（脚本会生成）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Port 8090 -Autostart
  powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Action status
#>
[CmdletBinding()]
param(
  [ValidateSet('install', 'start', 'stop', 'status', 'uninstall', 'token', 'doctor')]
  [string]$Action = 'install',
  [string]$DataDir = "$env:LOCALAPPDATA\airelay",
  [int]$Port = 8000,
  [switch]$Autostart,
  [switch]$NoDesktop   # 不装托盘依赖（只当后台服务跑）
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Venv = Join-Path $Root '.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName = 'airelay'

function Info($msg) { Write-Host "[中转站] $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "[注意] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "[失败] $msg" -ForegroundColor Red; exit 1 }

function Find-Python {
  foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
      $version = & $cmd.Source -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
      if ([version]$version -ge [version]'3.11') { return $cmd.Source }
    } catch { }
  }
  return $null
}

function Get-Token {
  if (-not (Test-Path $Py)) { return '' }
  return (& $Py -m airelay --data-dir $DataDir --print-token 2>$null)
}

function Start-Relay {
  Info "启动中转站（端口 $Port）"
  $exe = Join-Path $Venv 'Scripts\pythonw.exe'
  if (-not (Test-Path $exe)) { $exe = $Py }
  Start-Process -FilePath $exe -ArgumentList @('-m', 'airelay', '--data-dir', $DataDir, '--port', "$Port") `
    -WorkingDirectory $Root -WindowStyle Hidden
  Start-Sleep -Seconds 4
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 5
    Info "已启动：http://127.0.0.1:$Port/admin （健康检查 $($health.status)）"
  } catch {
    Warn "进程已拉起但健康检查未通过，请查看 $DataDir\logs\airelay.log"
  }
}

function Stop-Relay {
  Info '停止中转站'
  Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'airelay' } |
    ForEach-Object { Write-Host "  结束 PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
}

switch ($Action) {
  'token' { $t = Get-Token; if ($t) { Write-Host $t -ForegroundColor Green } else { Fail '尚未初始化，请先运行 install' } ; exit 0 }
  'status' {
    $procs = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
      Where-Object { $_.CommandLine -and $_.CommandLine -match 'airelay' }
    if ($procs) { Info "运行中（$($procs.Count) 个进程）"; try { Invoke-RestMethod "http://127.0.0.1:$Port/readyz" | ConvertTo-Json -Compress } catch { Warn '健康检查失败' } }
    else { Warn '未在运行' }
    exit 0
  }
  'stop' { Stop-Relay; exit 0 }
  'start' { Start-Relay; exit 0 }
  'doctor' { & $Py -m airelay --data-dir $DataDir --doctor; exit $LASTEXITCODE }
  'uninstall' {
    Stop-Relay
    Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
    Warn "已停止并移除自启。代码与数据保留在：$Root / $DataDir"
    exit 0
  }
}

# ------------------------------------------------------------------ install
$python = Find-Python
if (-not $python) { Fail '未找到 Python 3.11+，请先从 python.org 安装并勾选 Add to PATH' }
Info "使用 Python：$python"

if (-not (Test-Path $Py)) {
  Info "创建虚拟环境 $Venv"
  & $python -m venv $Venv
}

Info '安装依赖'
& $Py -m pip install --upgrade pip --quiet
$req = if ($NoDesktop) { 'requirements.txt' } else { 'requirements-desktop.txt' }
& $Py -m pip install -r (Join-Path $Root $req) --quiet

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

Info '环境自检'
& $Py -m airelay --data-dir $DataDir --doctor

# 生成双击启动脚本
$launcher = Join-Path $Root '启动中转站.cmd'
@"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
"$Py" -m airelay --data-dir "$DataDir" --port $Port
"@ | Set-Content -Path $launcher -Encoding UTF8
Info "已生成启动脚本：$launcher"

if ($Autostart) {
  Info '写入开机自启（当前用户）'
  $cmd = "`"$Py`" -m airelay --data-dir `"$DataDir`" --port $Port"
  Set-ItemProperty -Path $RunKey -Name $RunName -Value $cmd
} else {
  Warn '未开启开机自启；需要的话重新运行并加上 -Autostart'
}

Start-Relay
$token = Get-Token
Write-Host ''
Write-Host '────────────────────────────────────────────────────────────'
Write-Host ' 安装完成'
Write-Host '────────────────────────────────────────────────────────────'
Write-Host " 控制台        http://127.0.0.1:$Port/admin"
Write-Host " OpenAI 基地址 http://127.0.0.1:$Port/v1"
Write-Host " 管理员令牌    $token"
Write-Host " 数据目录      $DataDir"
Write-Host '────────────────────────────────────────────────────────────'
Write-Host ' 托盘图标里可以打开控制台、复制地址、看日志、退出。'
Write-Host ' 端口与监听地址也能在控制台「设置 → 网络」里改，会热重绑定。'
Write-Host '────────────────────────────────────────────────────────────'
