#Requires -Version 5.1
<#
.SYNOPSIS
    QMT 看门狗 · Windows 主机一键部署（云主机 / 物理机通用）

.DESCRIPTION
    做六件事：
      1. 找 Python（优先复用 QMT 自带的 3.6，找不到就装免安装版）
      2. 布置 C:\quant\watchdog 并写入配置（自动探测 QMT 主程序与日志路径）
      3. 开机自动登录（可选，GUI 程序必须有人登录才能跑）
      4. 电源常开 + 关掉 Windows 更新自动重启（半夜重启会把 QMT 干掉）
      5. QMT 加入开机启动
      6. 注册计划任务（每分钟跑一次 --once，比常驻进程抗崩）

.EXAMPLE
    .\bootstrap.ps1
    .\bootstrap.ps1 -AutoLogon -LogonUser Administrator -LogonPassword 'xxx'
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\quant',
    [string]$Source = '',              # 看门狗文件来源目录，默认取脚本同级
    [switch]$AutoLogon,                # 配置开机自动登录（无人值守必需）
    [string]$LogonUser = '',
    [string]$LogonPassword = '',
    [switch]$QmtAutoStart,             # 把 QMT 加进开机启动项
    [switch]$SkipPower,                # 跳过电源设置
    [switch]$SkipUpdateGuard,          # 跳过 Windows 更新防护
    [switch]$SkipTask                  # 只部署文件，不注册计划任务
)

$ErrorActionPreference = 'Stop'
$task = 'QMTWatchdog'

function Say($m, $c = 'Cyan') { Write-Host $m -ForegroundColor $c }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Ok($m) { Write-Host $m -ForegroundColor Green }

# ───────────────────────── 1. 找 QMT 目录 ─────────────────────────

function Find-QmtDirs {
    $hits = @()
    foreach ($r in @('C:\', 'D:\', 'E:\')) {
        if (Test-Path $r) {
            $hits += Get-ChildItem -Path $r -Directory -EA SilentlyContinue |
                     Where-Object { $_.Name -match 'QMT|迅投|Xt|通达信|THS|同花顺' }
        }
    }
    foreach ($r in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($r -and (Test-Path $r)) {
            $hits += Get-ChildItem -Path $r -Directory -EA SilentlyContinue |
                     Where-Object { $_.Name -match 'QMT|迅投|Xt' }
        }
    }
    return @($hits | Select-Object -ExpandProperty FullName -Unique)
}

# ───────────────────────── 2. 找 Python ─────────────────────────

function Test-PythonOk($p) {
    try {
        $v = & $p -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if (-not $v) { return $false }
        $parts = $v.Trim().Split('.')
        return ([int]$parts[0] -ge 3) -and ([int]$parts[1] -ge 6)
    } catch { return $false }
}

function Find-Python($dirs) {
    foreach ($d in $dirs) {
        $cand = Get-ChildItem -Path $d -Filter 'python.exe' -Recurse -Depth 4 -EA SilentlyContinue |
                Select-Object -First 5
        foreach ($c in @($cand)) { if (Test-PythonOk $c.FullName) { return $c.FullName } }
    }
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Python", 'C:\Python311', 'C:\Python312', 'C:\Python39')) {
        $cand = Get-ChildItem -Path $p -Filter 'python.exe' -Recurse -Depth 2 -EA SilentlyContinue |
                Select-Object -First 3
        foreach ($c in @($cand)) { if (Test-PythonOk $c.FullName) { return $c.FullName } }
    }
    $cmd = Get-Command python -EA SilentlyContinue
    if ($cmd -and (Test-PythonOk $cmd.Source)) { return $cmd.Source }
    return $null
}

function Install-PythonEmbed($root) {
    $dir = Join-Path $root 'py'
    $exe = Join-Path $dir 'python.exe'
    if ((Test-Path $exe) -and (Test-PythonOk $exe)) { return $exe }
    Say '  未找到可用 Python，下载免安装版（3.11 embed）…'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $url = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip'
    $zip = Join-Path $env:TEMP 'py311.zip'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 120
        Expand-Archive -Path $zip -DestinationPath $dir -Force
    } catch {
        Warn "  下载失败：$($_.Exception.Message)"
        Warn '  请手动安装 Python 3.6+ 后重新运行本脚本。'
        return $null
    }
    # embed 版默认不加载 site，标准库仍在 python311.zip，够用
    if (Test-PythonOk $exe) { return $exe }
    return $null
}

# ───────────────────────── 3. 找 QMT 主程序 ─────────────────────────

function Find-QmtExe($dirs) {
    $names = @('XtItClient.exe', 'XtTradeClient.exe', 'QMT.exe', 'XtMiniQmt.exe', 'XtQuant.exe')
    foreach ($d in $dirs) {
        foreach ($n in $names) {
            $hit = Get-ChildItem -Path $d -Filter $n -Recurse -Depth 3 -EA SilentlyContinue |
                   Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
    }
    return $null
}

function Find-LogGlobs($dirs) {
    $globs = @()
    foreach ($d in $dirs) {
        $g = Get-ChildItem -Path $d -Recurse -Depth 4 -Directory -EA SilentlyContinue |
             Where-Object { $_.Name -eq 'log' } | Select-Object -First 2
        foreach ($x in @($g)) { $globs += (Join-Path $x.FullName '*.log') }
    }
    return @($globs | Select-Object -Unique)
}

# ═══════════════════════ 主流程 ═══════════════════════

Say "═══ QMT 看门狗部署 · 根目录 $Root ═══"

if ([string]::IsNullOrWhiteSpace($Source)) { $Source = Join-Path $PSScriptRoot '..' }
$wd = Join-Path $Root 'watchdog'
New-Item -ItemType Directory -Force -Path $wd | Out-Null

Say '[1/6] 扫描 QMT 安装目录…'
$qmtDirs = Find-QmtDirs
if ($qmtDirs.Count -eq 0) {
    Warn '  没找到 QMT 目录（QMT 还没装？）。先继续，装完请重跑本脚本。'
} else {
    $qmtDirs | ForEach-Object { Ok "  命中：$_" }
}

Say '[2/6] 定位 Python…'
$py = Find-Python $qmtDirs
if (-not $py) { $py = Install-PythonEmbed $Root }
if (-not $py) { Warn '  仍然没有 Python，中止。'; exit 1 }
Ok "  使用：$py"

Say '[3/6] 部署文件…'
$need = @('watchdog.py', 'calendar.json', 'watchdog_config.example.json')
foreach ($f in $need) {
    $src = Join-Path $Source $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $wd $f) -Force }
}
foreach ($f in $need) {
    if (-not (Test-Path (Join-Path $wd $f))) { Warn "  缺少 $f（请放到 $Source 下）" }
}
Ok "  已部署到 $wd"

Say '[4/6] 写配置…'
$exe = Find-QmtExe $qmtDirs
$cfgPath = Join-Path $wd 'watchdog_config.json'
$base = Join-Path $wd 'watchdog_config.example.json'
if (Test-Path $cfgPath) {
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Say '  沿用已有配置'
} elseif (Test-Path $base) {
    $cfg = Get-Content $base -Raw -Encoding UTF8 | ConvertFrom-Json
    Say '  从模板生成'
} else { Warn '  配置模板缺失，跳过'; $cfg = $null }

if ($cfg) {
    if ($exe) {
        $cfg.qmt.exe_path = $exe
        $cfg.qmt.exe_cwd = Split-Path $exe -Parent
        Ok "  QMT 主程序：$exe"
    } else { Warn '  未找到 QMT 主程序，请手工填 qmt.exec_path' }

    $globs = Find-LogGlobs $qmtDirs
    if ($globs.Count -gt 0) {
        $merged = @($globs) + @($cfg.logs.globs) | Select-Object -Unique
        $cfg.logs.globs = $merged
        Ok ('  日志候选：{0} 条' -f $globs.Count)
    }
    ($cfg | ConvertTo-Json -Depth 8) | Set-Content $cfgPath -Encoding UTF8
}

Say '[5/6] 主机环境（无人值守三件套）…'

if ($AutoLogon) {
    $u = if ($LogonUser) { $LogonUser } else { $env:USERNAME }
    $p = $LogonPassword
    if (-not $p) {
        $sec = Read-Host "  请输入 $u 的登录密码（用于开机自动登录）" -AsSecureString
        $p = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
             [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
    $reg = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty $reg 'AutoAdminLogon' '1'
    Set-ItemProperty $reg 'DefaultUserName' $u
    Set-ItemProperty $reg 'DefaultPassword' $p
    Set-ItemProperty $reg 'AutoLogonCount' 0 -EA SilentlyContinue
    Ok "  已设置开机自动登录：$u"
    Warn '  ⚠ 密码以明文写入注册表 DefaultPassword，仅限专用交易机'
} else { Say '  跳过自动登录（无人值守场景请务必开启）' }

if (-not $SkipPower) {
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    powercfg /change monitor-timeout-ac 0 | Out-Null
    Ok '  电源：永不休眠'
}

if (-not $SkipUpdateGuard) {
    $au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
    New-Item -Path $au -Force | Out-Null
    Set-ItemProperty $au 'NoAutoRebootWithLoggedOnUsers' 1
    Set-ItemProperty $au 'AUOptions' 3          # 下载并通知，不自动重启
    Ok '  Windows 更新：不再自动重启'
}

if ($QmtAutoStart -and $exe) {
    $run = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run'
    Set-ItemProperty $run 'QMT' "`"$exe`""
    Ok "  QMT 已加入开机启动：$exe"
}

Say '[6/6] 注册计划任务…'
if ($SkipTask) { Say '  已跳过' }
else {
    $wdpy = Join-Path $wd 'watchdog.py'
    $tr = "`"$py`" `"$wdpy`" --once"
    $u = if ($LogonUser) { $LogonUser } else { "$env:USERDOMAIN\$env:USERNAME" }
    $args = @('/create', '/tn', $task, '/tr', $tr, '/sc', 'MINUTE', '/mo', '1',
              '/rl', 'HIGHEST', '/f', '/ru', $u, '/it')
    if ($LogonPassword) { $args += @('/rp', $LogonPassword) }
    $out = & schtasks $args 2>&1
    if ($LASTEXITCODE -eq 0) {
        Ok "  已注册：$task（每分钟一次，用户 $u）"
    } else {
        Warn "  注册失败（exit=$LASTEXITCODE）：$out"
        Warn '  多半是没输密码。可手工执行：'
        Warn "  schtasks /create /tn $task /tr `"$tr`" /sc MINUTE /mo 1 /rl HIGHEST /f"
    }
    & schtasks /run /tn $task 2>&1 | Out-Null
}

Say ''
Say '═══ 部署完成，请按顺序确认 ═══' 'Green'
Say "  1. python `"$py`" `"$wd\watchdog.py`" --probe-logs   → 看日志路径是否命中"
Say "  2. python `"$py`" `"$wd\watchdog.py`" --status       → 看三级心跳"
Say '  3. 杀掉 QMT 进程，等 6 分钟，看是否自动重启并收到告警'
Say '  4. 填好 alert.webhook（企微机器人）后重跑一次'
Say "  配置文件：$cfgPath"
