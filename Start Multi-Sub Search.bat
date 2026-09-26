@echo off
cd /d "%~dp0"
echo Starting Multi-Sub Reddit Search...
where py >nul 2>&1 && (
  py -3 multi-sub-search-server.py
  goto :eof
)
where python >nul 2>&1 && (
  python multi-sub-search-server.py
  goto :eof
)
echo Python not found. Install Python 3, then run this again.
pause
