@echo off
rem Рабочий воркер AI-Hub на ноутбуке.
rem Проверки выполняются изолированным Linux-раннером в WSL-дистрибутиве AIHub:
rem wsl.exe запускает tools/docker_check_runner.py, снимок проекта пробрасывается
rem через windows_roots в конфиге раннера. Без AIHUB_ISOLATED_RUNNER_ARGV
rem воркер честно не объявляет run_checks.
setlocal
set HUB_URL=http://127.0.0.1:8102
set AIHUB_WORKSPACE_DIR=C:\Users\user\AppData\Local\aihub\workspaces
set QWEN_JS=C:\Users\user\bin\qwen.js
set AIHUB_ISOLATED_RUNNER_ARGV=["C:\\Windows\\System32\\wsl.exe","-d","AIHub","--","python3","/home/hub/aihub/tools/docker_check_runner.py","--config","/home/hub/aihub/runner.json"]
if exist "%~dp0token.txt" (
  set /p BRIDGE_TOKEN=<"%~dp0token.txt"
) else (
  for /f "usebackq tokens=2 delims==" %%A in (`findstr /b "TEST_HUB_TOKEN=" "R:\AI-Hub\workspace\flow-finish-0443c2a4\stand.env"`) do set BRIDGE_TOKEN=%%A
)
cd /d "%~dp0"
"C:\Users\user\AppData\Local\Programs\Python\Python312\python.exe" worker_v2.py
endlocal