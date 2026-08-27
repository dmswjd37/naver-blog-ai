@echo off

set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "PROFILE_PATH=%LOCALAPPDATA%\naver-blog-ai\chrome-profile"

if not exist "%CHROME_PATH%" (
    set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)

if not exist "%CHROME_PATH%" (
    echo Chrome을 찾을 수 없습니다.
    pause
    exit /b 1
)

start "" "%CHROME_PATH%" ^
    --remote-debugging-port=9222 ^
    --user-data-dir="%PROFILE_PATH%" ^
    "https://blog.naver.com/dmswjddid37"