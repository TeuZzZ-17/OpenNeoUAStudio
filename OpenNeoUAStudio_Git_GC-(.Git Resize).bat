@echo off
setlocal
title OPENNEOUA STUDIO - GIT GC SICURO

rem Lavora nella cartella in cui si trova questo file BAT
pushd "%~dp0" >nul 2>&1

echo ============================================================
echo       OPENNEOUA STUDIO - MANUTENZIONE GIT SICURA
echo ============================================================
echo.
echo Cartella:
echo %CD%
echo.

rem Verifica che Git sia disponibile
where git >nul 2>&1
if errorlevel 1 (
    echo ERRORE: Git non e' disponibile nel PATH.
    echo Apri Git Bash oppure installa/configura Git per Windows.
    echo.
    pause
    popd
    exit /b 1
)

rem Verifica che la cartella sia un repository Git
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo ERRORE: questa cartella non contiene un repository Git valido.
    echo Metti questo file BAT nella cartella principale del progetto,
    echo cioe' quella che contiene la cartella nascosta .git
    echo.
    pause
    popd
    exit /b 1
)

echo Repository rilevato:
git rev-parse --show-toplevel
echo.

echo Stato Git attuale:
git status --short
echo.

echo Dimensioni PRIMA della manutenzione:
git count-objects -vH
echo.

echo Questa operazione eseguira' soltanto:
echo     git gc
echo.
echo Non modifica il sorgente e non riscrive la cronologia.
echo.

choice /C SN /N /M "Procedere? [S/N]: "
if errorlevel 2 (
    echo.
    echo Operazione annullata.
    echo.
    pause
    popd
    exit /b 0
)

echo.
echo Manutenzione Git in corso...
echo Non chiudere questa finestra.
echo.

git gc
if errorlevel 1 (
    echo.
    echo ERRORE: git gc non e' terminato correttamente.
    echo Nessun comando aggressivo e' stato eseguito.
    echo.
    pause
    popd
    exit /b 1
)

echo.
echo Dimensioni DOPO la manutenzione:
git count-objects -vH
echo.

echo ============================================================
echo Operazione completata con successo.
echo ============================================================
echo.
pause

popd
endlocal
