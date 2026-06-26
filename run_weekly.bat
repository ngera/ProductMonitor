@echo off
REM Weekly pipeline run (DESIGN.md §13 appendix). Assumes Foundry Local is up.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

python -m pipeline.run %*

echo.
echo Done. Open the report under reports\^<week^>\index.html
endlocal
