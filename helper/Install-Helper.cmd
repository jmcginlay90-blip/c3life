@echo off
rem Double-click to install the C3 Life helper on this PC. Windows asks once for permission (for the firewall rule).
rem It runs Install-Helper.ps1, next to this file, for you (the person logged in).
set "ME=%USERDOMAIN%\%USERNAME%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0Install-Helper.ps1\" -User \"%ME%\"'"
