# Panel

Браузерная панель управления контейнерами с кодом — упрощённый клон Pterodactyl для запуска кода на разных языках (Python, Node.js, Bun, Go, bash, и любых других через кастомные Docker-образы).

## Возможности

- Регистрация / вход с JWT-cookie
- Создание контейнеров-"серверов" из шаблонов (eggs)
- Power-actions: start / stop / restart / kill / rebuild
- Live-консоль через WebSocket (стрим логов + exec команд)
- Файловый менеджер (просмотр, редактирование, создание, удаление)
- Лимиты CPU и памяти на контейнер
- Админ-панель: пользователи, шаблоны, инфо о Docker
- Тёмная тема в стиле Pterodactyl

## Стек

- Backend: FastAPI + SQLAlchemy + SQLite + Docker SDK
- Frontend: ванильный JS + Jinja2 шаблоны + кастомный CSS
- Контейнеризация задач: Docker

## Запуск на Windows (для тестирования)

Требования: Python 3.10+, Docker Desktop (опционально — для управления контейнерами).

```cmd
run.bat
```

Откройте http://localhost:8080. Логин по умолчанию: `admin` / `admin`.

Без Docker UI работает, но кнопки power и консоль выдадут ошибку.

## Установка на Ubuntu 24.04 (production)

```bash
sudo ./install-ubuntu.sh
```

Скрипт:
- ставит Docker (если нет)
- создаёт системного пользователя `panel`
- копирует файлы в `/opt/panel`
- настраивает systemd-сервис `panel.service`
- генерирует `PANEL_SECRET`

Управление:
```bash
systemctl status panel
systemctl restart panel
journalctl -u panel -f
```

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PANEL_SECRET` | `change-me-...` | Секрет JWT |
| `PANEL_DB` | `sqlite:///./panel.db` | DSN базы (SQLAlchemy) |
| `PANEL_DATA_ROOT` | `./data/servers` | Куда монтируются `/home/container` |

## Структура

```
panel/
  backend/
    main.py              FastAPI приложение и роуты
    auth.py              JWT, bcrypt, зависимости авторизации
    database.py          SQLAlchemy модели и init
    docker_manager.py    Обёртка над Docker SDK
    files.py             Файловый менеджер с защитой от path traversal
    requirements.txt
  frontend/
    templates/           Jinja2: login, register, dashboard, server, admin
    static/
      css/style.css      Тёмная тема
      js/app.js          API-клиент
  run.bat / run.sh       Запуск для разработки
  install-ubuntu.sh      Установщик для Ubuntu 24
```

## Безопасность

В production обязательно:
- Сменить пароль `admin` сразу после первого входа
- Поставить `PANEL_SECRET` через окружение (install-ubuntu.sh делает автоматически)
- Поставить reverse-proxy (nginx + TLS) перед панелью
- Ограничить доступ к Docker-сокету только пользователю `panel`

## Расширение

Добавить новый язык — через UI Администрирование → «Новый шаблон»:
- Название, язык
- Docker-образ (любой публичный или локальный)
- Команда запуска (выполняется в `/home/container` через `sh -c`)
