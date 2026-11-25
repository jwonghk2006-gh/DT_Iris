@echo off
rem Runner in lbpaper mode (Longbridge DEMO account: real orders, demo money).
rem Started by a one-off Windows task; keep this window open until 15:00 New York (04:00 Singapore):
rem stops are watched by the runner, so closing it leaves open positions without a stop.
cd /d "%~dp0"
if not exist logs mkdir logs
echo ==== %date% %time% lbpaper ==== >> logs\runner.log
".venv\Scripts\python.exe" -u dt_runner.py --mode lbpaper >> logs\runner.log 2>&1
echo exit code %errorlevel% >> logs\runner.log
