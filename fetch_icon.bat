@echo off
:: Downloads the current game icon to output\icon.png (e.g. for the Discord Developer Portal)
if not exist "%~dp0output\" mkdir "%~dp0output"
pushd "%~dp0output"
python -B "%~dp0src\main.py" --fetch-icon
popd
