@echo off
cd /d "%~dp0"
echo Starting DT_Iris dashboard at http://localhost:8518 ...
echo Keep this window open while you use the dashboard. Close it to stop.
".venv\Scripts\python.exe" -m streamlit run dashboard.py --server.port 8518 --browser.gatherUsageStats false
echo.
echo The dashboard stopped. If you see "Port 8518 is in use", another copy is already running:
echo open http://localhost:8518 in your browser instead.
pause
