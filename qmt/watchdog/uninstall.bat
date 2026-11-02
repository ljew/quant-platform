@echo off
chcp 65001 >nul
REM ============================================================
REM  QMT 看门狗 · 卸载（停进程 + 删计划任务）
REM  用法：右键 → 以管理员身份运行
REM ============================================================

setlocal
set WD_DIR=%~dp0
set TASK_NAME=QMTWatchdog

echo [1/3] 停止计划任务
schtasks /end /tn "%TASK_NAME%" >nul 2>&1

echo [2/3] 结束看门狗进程
taskkill /F /FI "WINDOWTITLE eq *watchdog*" >nul 2>&1
for /f "tokens=2" %%p in ('tasklist /FI "IMAGENAME eq pythonw.exe" /NH ^| findstr /I "pythonw"') do (
    echo   发现 pythonw 进程 %%p（如果是看门狗，请手动在任务管理器结束）
)

echo [3/3] 删除计划任务
schtasks /delete /tn "%TASK_NAME%" /f

echo.
echo 已卸载。watchdog.log / watchdog_state.json 保留在本目录，便于事后排查；
echo 不需要就手动删掉。
pause
