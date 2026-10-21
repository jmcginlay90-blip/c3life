@echo off
rem Double-click to take the C3 Life helper off this PC. Windows asks once for permission (for the firewall rule).
set "ME=%USERDOMAIN%\%USERNAME%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0Uninstall-Helper.ps1\" -User \"%ME%\"'"
