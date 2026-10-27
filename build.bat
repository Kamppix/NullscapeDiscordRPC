@echo off
setlocal
cd /d "%~dp0"

:: Don't leave __pycache__ folders next to the sources
set PYTHONDONTWRITEBYTECODE=1

:: Names and version come from common.py (single source of truth)
set APP_NAME=
set UNINSTALL_NAME=
set APP_VERSION=
for /f "usebackq delims=" %%v in (`python -c "import common; print(common.APP_NAME)"`) do set APP_NAME=%%v
for /f "usebackq delims=" %%v in (`python -c "import common, os; print(os.path.splitext(common.UNINSTALL_EXE)[0])"`) do set UNINSTALL_NAME=%%v
for /f "usebackq delims=" %%v in (`python -c "import common; print(common.APP_VERSION)"`) do set APP_VERSION=%%v
if not defined APP_NAME (
    echo Could not read APP_NAME from common.py
    goto :fail
)
if not defined UNINSTALL_NAME (
    echo Could not read UNINSTALL_EXE from common.py
    goto :fail
)
if not defined APP_VERSION (
    echo Could not read APP_VERSION from common.py
    goto :fail
)

:: Bitness of the Python you build with (the exes inherit it): x64, x86 or arm64
set ARCH=
for /f "usebackq delims=" %%a in (`python -c "import platform,sys; m=platform.machine().upper(); print('x86' if sys.maxsize<2**32 else 'arm64' if m=='ARM64' else 'x64')"`) do set ARCH=%%a
if not defined ARCH set ARCH=x64

set SETUP_NAME=%APP_NAME%-%APP_VERSION%-setup-%ARCH%.exe
echo Building version %APP_VERSION% (%ARCH%) -^> %SETUP_NAME%

echo Installing build dependencies...
python -m pip install --upgrade pyinstaller pypresence psutil requests pillow || goto :fail

echo Fetching the latest game icon...
python main.py --fetch-icon || goto :fail

echo Building %APP_NAME% (the app)...
:: Not --onefile: the app goes into the install folder as an exe plus _internal, so it never unpacks to %TEMP%
python -m PyInstaller --noconfirm --clean --noconsole --name %APP_NAME% --icon icon.png --exclude-module tkinter --hidden-import PIL.IcoImagePlugin --hidden-import PIL.PngImagePlugin main.py || goto :fail

echo Building %UNINSTALL_NAME%.exe...
python -m PyInstaller --noconfirm --onefile --noconsole --uac-admin --name %UNINSTALL_NAME% --icon icon.png --add-data "icon.png;." uninstaller.py || goto :fail

echo Building the installer...
python -m PyInstaller --noconfirm --onefile --noconsole --uac-admin --name %APP_NAME%_Setup --icon icon.png --hidden-import PIL.IcoImagePlugin --hidden-import PIL.PngImagePlugin --add-data "icon.png;." --add-data "dist\%APP_NAME%;payload" --add-data "dist\%UNINSTALL_NAME%.exe;payload" installer.py || goto :fail

echo Cleaning up...
move /y dist\%APP_NAME%_Setup.exe "%SETUP_NAME%"
rmdir dist\ /s /q
rmdir build\ /s /q
if exist __pycache__\ rmdir __pycache__\ /s /q
del icon.png /q
del %APP_NAME%.spec /q
del %UNINSTALL_NAME%.spec /q
del %APP_NAME%_Setup.spec /q

echo.
echo Build finished successfully!  ^>^>  %SETUP_NAME%
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
if exist __pycache__\ rmdir __pycache__\ /s /q
if exist icon.png del icon.png /q
pause
exit /b 1
