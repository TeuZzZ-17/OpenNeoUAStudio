@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title OpenNeoUAStudio - Build Windows

echo ============================================================
echo        OpenNeoUAStudio - Build automatico Windows
echo ============================================================
echo.
echo Cartella progetto:
echo %CD%
echo.

REM ------------------------------------------------------------
REM Trova Python: prima "py", poi "python", poi "python3"
REM ------------------------------------------------------------
set "PYTHON_CMD="

where py >nul 2>&1
if %errorlevel%==0 set "PYTHON_CMD=py"

if not defined PYTHON_CMD (
    where python >nul 2>&1
    if %errorlevel%==0 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    where python3 >nul 2>&1
    if %errorlevel%==0 set "PYTHON_CMD=python3"
)

if not defined PYTHON_CMD (
    echo [ERRORE] Python non e' installato oppure non e' nel PATH.
    echo.
    echo Installa Python per Windows e durante l'installazione abilita:
    echo    "Add python.exe to PATH"
    echo.
    pause
    exit /b 1
)

echo [OK] Python trovato:
%PYTHON_CMD% --version
echo.

REM ------------------------------------------------------------
REM Controlla pip
REM ------------------------------------------------------------
%PYTHON_CMD% -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] pip non trovato. Provo ad abilitarlo...
    %PYTHON_CMD% -m ensurepip --upgrade
    if errorlevel 1 goto :error
)

REM ------------------------------------------------------------
REM Installa dipendenze del progetto, se presenti
REM ------------------------------------------------------------
if exist "requirements.txt" (
    echo [INFO] Installazione requirements.txt...
    %PYTHON_CMD% -m pip install -r "requirements.txt"
    if errorlevel 1 goto :error
)

REM ------------------------------------------------------------
REM Installa/aggiorna PyInstaller
REM ------------------------------------------------------------
echo.
echo [INFO] Controllo PyInstaller...
%PYTHON_CMD% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error

REM ------------------------------------------------------------
REM Verifica file necessari
REM ------------------------------------------------------------
if not exist "OpenNeoUAStudio.spec" (
    echo.
    echo [ERRORE] OpenNeoUAStudio.spec non trovato.
    echo Metti questo BAT nella cartella principale di OpenNeoUAStudio.
    goto :error_pause
)

if not exist "icons\OpenNeoUAStudio.ico" (
    echo.
    echo [ERRORE] Icona non trovata:
    echo icons\OpenNeoUAStudio.ico
    goto :error_pause
)

REM ------------------------------------------------------------
REM Build
REM Lo .spec incorpora gia' icons\OpenNeoUAStudio.ico
REM ------------------------------------------------------------
echo.
echo ============================================================
echo [BUILD] Compilazione OpenNeoUAStudio.exe...
echo ============================================================
echo.

%PYTHON_CMD% -m PyInstaller --clean --noconfirm "OpenNeoUAStudio.spec"
if errorlevel 1 goto :error

if not exist "dist\OpenNeoUAStudio.exe" (
    echo.
    echo [ERRORE] La build e' terminata ma dist\OpenNeoUAStudio.exe non esiste.
    goto :error_pause
)

echo.
echo ============================================================
echo [SUCCESSO]
echo Creato:
echo %CD%\dist\OpenNeoUAStudio.exe
echo.
echo Icona incorporata:
echo %CD%\icons\OpenNeoUAStudio.ico
echo ============================================================
echo.

start "" "%CD%\dist"
pause
exit /b 0

:error
echo.
echo [ERRORE] La compilazione si e' interrotta.
echo Controlla il messaggio mostrato sopra.
echo.

:error_pause
pause
exit /b 1
