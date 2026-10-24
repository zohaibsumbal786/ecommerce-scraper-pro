@echo off
echo ========================================
echo   eCommerce Scraper Pro - Server Start
echo ========================================
echo.
echo Starting server on http://localhost:8000
echo.
echo IMPORTANT: After server starts, open your browser and:
echo   1. Go to http://localhost:8000
echo   2. Press Ctrl+Shift+R to hard refresh (clear cache)
echo   3. Click any store and test "Export All CSV" button
echo.
echo Press Ctrl+C to stop the server
echo ========================================
echo.

cd /d "%~dp0"
C:\Users\zohai\AppData\Local\Programs\Python\Python313\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

pause
