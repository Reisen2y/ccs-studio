@echo off
REM 打包免 Python 环境的单文件 exe（需要先: pip install pyinstaller）
python -m PyInstaller --noconfirm --onefile --windowed --collect-all customtkinter --name ccs-studio ccs_gui.pyw
echo.
echo 输出: dist\ccs-studio.exe
pause
