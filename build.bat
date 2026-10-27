@echo off
setlocal
cd /d "%~dp0"

:: Don't leave __pycache__ folders next to the sources
set PYTHONDONTWRITEBYTECODE=1
:: The sources live in src\ (this lets the "import common" one-liners below find them)
set PYTHONPATH=%~dp0src

:: Everything the build produces goes in output\ (its scratch files, including the downloaded
:: game icon, in output\temp\, deleted at the end). Paths are absolute because PyInstaller resolves
:: --icon and --add-data relative to the spec file, which lives in output\temp\.
set OUT=%~dp0output
set BUILD_TEMP=%OUT%\temp
set DIST=%BUILD_TEMP%\dist
set ICON=%BUILD_TEMP%\icon.png

:: Names and version come from src\common.py (single source of truth)
set APP_NAME=
set UNINSTALL_NAME=
set APP_VERSION=
for /f "usebackq delims=" %%v in (`python -c "import common; print(common.APP_NAME)"`) do set APP_NAME=%%v
for /f "usebackq delims=" %%v in (`python -c "import common, os; print(os.path.splitext(common.UNINSTALL_EXE)[0])"`) do set UNINSTALL_NAME=%%v
for /f "usebackq delims=" %%v in (`python -c "import common; print(common.APP_VERSION)"`) do set APP_VERSION=%%v
if not defined APP_NAME (
    echo Could not read APP_NAME from src\common.py
    goto :fail
)
if not defined UNINSTALL_NAME (
    echo Could not read UNINSTALL_EXE from src\common.py
    goto :fail
)
if not defined APP_VERSION (
    echo Could not read APP_VERSION from src\common.py
    goto :fail
)

:: Bitness of the Python you build with (the exes inherit it): x64, x86 or arm64
set ARCH=
for /f "usebackq delims=" %%a in (`python -c "import platform,sys; m=platform.machine().upper(); print('x86' if sys.maxsize<2**32 else 'arm64' if m=='ARM64' else 'x64')"`) do set ARCH=%%a
if not defined ARCH set ARCH=x64

set SETUP_NAME=%APP_NAME%-%APP_VERSION%-setup-%ARCH%.exe
set PORTABLE_BASE=%APP_NAME%-%APP_VERSION%-portable-%ARCH%
echo Building version %APP_VERSION% (%ARCH%) -^> output\%SETUP_NAME% and output\%PORTABLE_BASE%.zip

set PYI=python -m PyInstaller --noconfirm --noconsole --distpath "%DIST%" --workpath "%BUILD_TEMP%\build" --specpath "%BUILD_TEMP%"

if not exist "%OUT%\" mkdir "%OUT%" || goto :fail
if exist "%BUILD_TEMP%\" rmdir "%BUILD_TEMP%" /s /q
mkdir "%BUILD_TEMP%" || goto :fail

echo Installing build dependencies...
python -m pip install --upgrade pyinstaller pypresence psutil requests pillow || goto :fail

echo Fetching the latest game icon...
:: Only needed during the build (exe icons, window icons, the portable icon.ico); use
:: fetch_icon.bat to get a copy to keep
pushd "%BUILD_TEMP%"
python "%~dp0src\main.py" --fetch-icon
set FETCH_ERROR=%errorlevel%
popd
if not "%FETCH_ERROR%"=="0" goto :fail

echo Building %APP_NAME% (the app)...
:: Not --onefile: the app goes into the install folder as an exe plus _internal, so it never unpacks to %TEMP%
%PYI% --clean --name %APP_NAME% --icon "%ICON%" --exclude-module tkinter --hidden-import PIL.IcoImagePlugin --hidden-import PIL.PngImagePlugin "%~dp0src\main.py" || goto :fail

echo Building %UNINSTALL_NAME%.exe...
%PYI% --onefile --uac-admin --name %UNINSTALL_NAME% --icon "%ICON%" --add-data "%ICON%;." "%~dp0src\uninstaller.py" || goto :fail

echo Building the installer...
%PYI% --onefile --uac-admin --name %APP_NAME%_Setup --icon "%ICON%" --hidden-import PIL.IcoImagePlugin --hidden-import PIL.PngImagePlugin --add-data "%ICON%;." --add-data "%DIST%\%APP_NAME%;payload" --add-data "%DIST%\%UNINSTALL_NAME%.exe;payload" "%~dp0src\installer.py" || goto :fail

echo Packing the portable version...
:: icon.ico is made with the app's own converter, so the app sees it as up to date on first launch.
:: It's added only now, after the installer was built, so it never ends up in the installer payload.
python -c "import common; open(r'%DIST%\%APP_NAME%\icon.ico', 'wb').write(common.png_bytes_to_ico(open(r'%ICON%', 'rb').read()))" || goto :fail
if exist "%OUT%\%PORTABLE_BASE%.zip" del "%OUT%\%PORTABLE_BASE%.zip" /q
python -c "import shutil; shutil.make_archive(r'%OUT%\%PORTABLE_BASE%', 'zip', r'%DIST%\%APP_NAME%')" || goto :fail

echo Cleaning up...
move /y "%DIST%\%APP_NAME%_Setup.exe" "%OUT%\%SETUP_NAME%" >nul || goto :fail
rmdir "%BUILD_TEMP%" /s /q
if exist src\__pycache__\ rmdir src\__pycache__\ /s /q

echo.
echo Build finished successfully!  ^>^>  output\%SETUP_NAME%  and  output\%PORTABLE_BASE%.zip
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
if exist "%BUILD_TEMP%\" rmdir "%BUILD_TEMP%" /s /q
if exist src\__pycache__\ rmdir src\__pycache__\ /s /q
pause
exit /b 1
