@echo off
REM ============================================================
REM  preview.bat - view the QTPD site locally before deploying
REM  Lives in setup\ but serves the PROJECT ROOT (one level up)
REM  at http://localhost:8000
REM  Close this window (or press Ctrl+C) to stop the server.
REM  This does NOT touch git or production in any way.
REM ============================================================

cd /d "%~dp0.."

echo.
echo   QTPD local preview
echo   ------------------
echo   Serving project root at http://localhost:8000
echo   Close this window to stop the server.
echo.

start "" http://localhost:8000

py -m http.server 8000
if errorlevel 1 (
  echo.
  echo   "py" launcher not found - trying "python" instead...
  python -m http.server 8000
)

pause