@echo off
REM Запуск Panel на Windows (для тестирования)
cd /d "%~dp0"
if not exist ".venv" (
  echo [+] Создание venv...
  python -m venv .venv
)
call .venv\Scripts\activate
echo [+] Установка зависимостей...
pip install -q -r backend\requirements.txt
echo [+] Запуск Panel на http://localhost:8080
cd backend
python main.py
