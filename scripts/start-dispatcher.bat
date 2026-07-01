@echo off
REM Launch dispatcher with correct env vars for examplebrand-july-campaign board
set HERMES_KANBAN_BOARD=examplebrand-july-campaign
set HERMES_PROFILE=content-factory
set HERMES_KANBAN_DB=%LOCALAPPDATA%\hermes\kanban.db
set HERMES_KANBAN_WORKSPACES_ROOT=%LOCALAPPDATA%\hermes\kanban\workspaces

cd /d "C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard"

REM Kill old dispatcher if exists
taskkill /FI "WINDOWTITLE eq dispatcher*" 2>nul
taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq dispatcher*" 2>nul

REM Start dispatcher
start "dispatcher" python scripts\dispatcher.py

echo Dispatcher started with board=examplebrand-july-campaign
