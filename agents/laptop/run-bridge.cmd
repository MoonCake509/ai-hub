@echo off
rem Запуск AI-Hub bridge в фоне без окна
cd /d C:\Users\lavy\aihub-bridge
start "" "C:\Users\lavy\AppData\Local\Programs\Python\Python312\pythonw.exe" "C:\Users\lavy\aihub-bridge\worker_v2.py"
