@echo off
rem Nightly recorder: saves the last US session's gappers' minute bars (see dt_record.py).
rem Run by the Windows scheduled task "DT_Iris nightly recorder" at 06:00 Singapore, Tue-Sat.
cd /d "%~dp0"
if not exist logs mkdir logs
echo ==== %date% %time% ==== >> logs\record.log
".venv\Scripts\python.exe" dt_record.py >> logs\record.log 2>&1
echo exit code %errorlevel% >> logs\record.log
