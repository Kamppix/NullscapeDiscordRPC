@echo off
setlocal
cd /d "%~dp0"

:: Builds the Linux AppImage (output\<APP_NAME>-<version>-x86_64.AppImage) from Windows, by running
:: build.sh in a Linux (Debian) container. Needs Docker Desktop to be running.
:: The program needs at least the C library version of that Debian (glibc 2.36: Ubuntu 23.04,
:: Debian 12, Fedora 37 or newer).
set IMAGE=python:3.14-bookworm

docker info >nul 2>&1
if errorlevel 1 (
    echo Docker isn't running - start Docker Desktop first.
    goto :fail
)
if not exist output\tools\ mkdir output\tools || goto :fail

echo Building the Linux AppImage in a %IMAGE% container...
:: output\tools is shared with the container so appimagetool is only downloaded once
docker run --rm -v "%CD%:/repo:ro" -v "%CD%\output:/out" -v "%CD%\output\tools:/build/output/tools" %IMAGE% bash -c ^
  "set -e; cp -r /repo/src /repo/build.sh /build/; cd /build; bash build.sh; cp output/*.AppImage /out/" || goto :fail

echo.
echo AppImage copied to output\
pause
exit /b 0

:fail
echo.
echo Linux build failed - see the messages above.
pause
exit /b 1
