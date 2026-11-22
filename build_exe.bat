@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ========================================================
echo   作品鉴赏downloader V1.0.1 - 本地透明构建脚本
echo ========================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.11+ 并勾选 Add to PATH。
    exit /b 1
)

echo [1/4] 安装构建依赖...
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1

echo [2/4] 准备内置运行库（FFmpeg / Node.js / redbook）...
python scripts\prepare_runtime.py
if errorlevel 1 exit /b 1

echo [3/4] 运行隐私审计与全量单元测试...
python scripts\privacy_audit.py
if errorlevel 1 exit /b 1
python -m unittest discover -s tests -p "test_*.py"
if errorlevel 1 exit /b 1

echo [4/4] 使用 PyInstaller 构建单文件 EXE...
python -m PyInstaller --clean --noconfirm build.spec
if errorlevel 1 exit /b 1

echo.
echo 构建完成：dist\作品鉴赏downloader-V1.0.1.exe
endlocal
