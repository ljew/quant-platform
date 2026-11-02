@echo off
chcp 65001 >nul
REM ============================================================
REM  QMT 看门狗 · 安装（开机自启）
REM
REM  用法：右键本文件 → 以管理员身份运行
REM  装完会在 Windows 计划任务里多出一条 QMTWatchdog，登录后自动拉起。
REM  第一次装之前，先把 watchdog_config.example.json 复制成
REM  watchdog_config.json 并填好 qmt.exe_path 与 alert.webhook。
REM ============================================================

setlocal

REM ↓↓↓ 唯一需要改的一行：python.exe / pythonw.exe 的完整路径 ↓↓↓
set PYTHON_EXE=C:\Python311\pythonw.exe
set PYTHON_EXE_ONCE=C:\Python311\python.exe

set WD_DIR=%~dp0
set WD_PY=%WD_DIR%watchdog.py
set TASK_NAME=QMTWatchdog

echo 工作目录：%WD_DIR%
echo 解释器  ：%PYTHON_EXE%

if not exist "%PYTHON_EXE%" (
    echo [错误] 找不到 %PYTHON_EXE%
    echo 请用记事本打开本文件，把 PYTHON_EXE 改成你机器上的真实路径。
    echo 不知道路径就开一个 cmd，执行：where python
    pause
    exit /b 1
)

if not exist "%WD_DIR%watchdog_config.json" (
    echo [错误] 缺少 watchdog_config.json
    echo 先把 watchdog_config.example.json 复制一份改名为 watchdog_config.json，
    echo 填好 qmt.exe_path（QMT 客户端主程序完整路径）。
    pause
    exit /b 1
)

echo.
echo [1/3] 先跑一次日志路径探测（确认下面列出的路径有没有命中）
"%PYTHON_EXE_ONCE%" "%WD_PY%" --probe-logs
echo.

echo [2/3] 注册计划任务（登录时自动启动）
schtasks /create /tn "%TASK_NAME%" /tr "\"%PYTHON_EXE%\" \"%WD_PY%\"" /sc ONLOGON /rl HIGHEST /f
if errorlevel 1 (
    echo [错误] 创建计划任务失败，请以管理员身份重新运行本文件
    pause
    exit /b 1
)

echo [3/3] 立即启动一次
schtasks /run /tn "%TASK_NAME%"

echo.
echo 完成。验证三件事：
echo   1. 任务管理器里能看到 pythonw.exe 常驻
echo   2. 本目录下 watchdog.log 有内容
echo   3. python "%WD_PY%" --status 能看到三级心跳
echo.
echo 卸载：运行 uninstall.bat
pause
