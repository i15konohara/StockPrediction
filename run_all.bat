@echo off
REM Run collector.py -> predictor.py -> site_generator.py in sequence.
REM Used for scheduled execution (e.g. Windows Task Scheduler).
REM Logs all output to run_all.log since scheduled runs (SYSTEM account) have no console.
REM Puts the PC back to sleep when finished, whether it succeeded or failed.
setlocal
cd /d "%~dp0"
set LOGFILE=%~dp0run_all.log
set RESULT=0

echo ==== %date% %time% : run_all.bat start ==== >> "%LOGFILE%"

where python >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH >> "%LOGFILE%"
    set RESULT=1
    goto :finish
)

python collector.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] collector.py failed >> "%LOGFILE%"
    set RESULT=1
    goto :finish
)

python predictor.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] predictor.py failed >> "%LOGFILE%"
    set RESULT=1
    goto :finish
)

python site_generator.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] site_generator.py failed >> "%LOGFILE%"
    set RESULT=1
    goto :finish
)

git add -A >> "%LOGFILE%" 2>&1
git commit -m "Automated update %date% %time%" >> "%LOGFILE%" 2>&1
git push origin main >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo [WARN] git push failed - site not published this run >> "%LOGFILE%"
) else (
    echo [INFO] published to GitHub Pages >> "%LOGFILE%"
)

echo ==== %date% %time% : run_all.bat completed successfully ==== >> "%LOGFILE%"

:finish
if not "%RESULT%"=="0" (
    echo ==== %date% %time% : run_all.bat FAILED ==== >> "%LOGFILE%"
)

echo ==== %date% %time% : putting the PC to sleep ==== >> "%LOGFILE%"
rundll32.exe powrprof.dll,SetSuspendState 0,1,0

exit /b %RESULT%
