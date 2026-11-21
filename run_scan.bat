@echo off
rem Pre-market scanner: waits until 09:00 New York, scans the whole Longbridge universe and locks today's watchlist.
rem Run by the Windows scheduled task "DT_Iris pre-market scan" at 20:45 Singapore, Mon-Fri (see dt_scanner.py).
cd /d "%~dp0"
if not exist logs mkdir logs
echo ==== %date% %time% ==== >> logs\scan.log
".venv\Scripts\python.exe" dt_scanner.py --scheduled >> logs\scan.log 2>&1
echo exit code %errorlevel% >> logs\scan.log
