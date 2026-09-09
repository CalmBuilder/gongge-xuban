@echo off
setlocal
"%~dp0node.exe" "%~dp0node_modules\@jackwener\opencli\dist\src\main.js" %*
exit /b %ERRORLEVEL%
