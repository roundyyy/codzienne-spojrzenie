@echo off
REM Codzienne Spojrzenie - local preview.
REM Downloads the current edition (news + images) into data\edition.js,
REM then serves the site at http://localhost:8000
cd /d "%~dp0"
echo Pobieranie wydania...
python scripts\build_edition.py
echo.
echo Strona dostepna pod adresem: http://localhost:8000
echo (Ctrl+C aby zatrzymac)
start "" http://localhost:8000
python -m http.server 8000
