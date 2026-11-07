@echo off
start /min "" H:\sanad_erp\run_server.bat
timeout /t 3 >nul
start msedge --app=http://localhost:8501