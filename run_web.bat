@echo off
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\Python\bin\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" web_server.py
) else (
    start "" pythonw web_server.py
)
