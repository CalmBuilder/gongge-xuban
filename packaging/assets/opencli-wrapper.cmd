@echo off
setlocal
set "NODE=%~dp0node.exe"
if not exist "%NODE%" set "NODE=%~dp0..\youtube-js\node.exe"
if not exist "%NODE%" (
  echo Bundled OpenCLI Node.js runtime is missing. 1>&2
  exit /b 2
)
"%NODE%" "%~dp0node_modules\@jackwener\opencli\dist\src\main.js" %*
exit /b %ERRORLEVEL%
