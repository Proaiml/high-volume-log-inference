@echo off
setlocal
cd /d "%~dp0"
title Log Cikarim Prototipi

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python bulunamadi.
  echo   https://www.python.org/downloads/ adresinden Python 3.10 veya uzerini kurun.
  echo   Kurulumda "Add python.exe to PATH" kutusunu isaretlemeyi unutmayin.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [Ilk kurulum] Sanal ortam olusturuluyor...
  python -m venv .venv
  if errorlevel 1 (
    echo Sanal ortam olusturulamadi.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "import anthropic" >nul 2>nul
if errorlevel 1 (
  echo [Kurulum] Claude baglantisi icin anthropic paketi kuruluyor ^(internet gerekir^)...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --retries 1 --timeout 15 -r requirements.txt
  if errorlevel 1 echo [Uyari] Paket kurulamadi. Uygulama simulasyon modunda calisir; Claude icin internetle tekrar baslatin.
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause
