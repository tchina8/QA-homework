# CLAUDE.md

## Python: только из .venv по прямому пути

В этом окружении `python`, `py` и `pytest` **не видны из PATH**: `python` в PATH —
это заглушка-алиас Microsoft Store (App Execution Alias), а `py`/`pytest` отсутствуют.
Чинить PATH не нужно — работаем через виртуальное окружение.

**Правило (действует и для субагентов, и для новых сессий):**
все команды `python` и `pytest` вызывать **только из `.venv` по прямому пути**.

```
.venv\Scripts\python.exe
.venv\Scripts\pytest.exe
```

Примеры:

```powershell
& ".venv\Scripts\python.exe" --version
& ".venv\Scripts\pytest.exe" -q
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
```

Запрещено: голые `python`, `py`, `pytest`, `pip`; `activate`-скрипты (состояние
оболочки между вызовами инструментов не сохраняется); правка PATH, в том числе
подстановка путей из реестра.

## Пересоздание окружения

Венв (`.venv/`) в git не хранится — он в `.gitignore`. Состав зависимостей —
в `requirements.txt`.

```powershell
& "C:\Users\User\AppData\Local\Programs\Python\Launcher\py.exe" -3.13 -m venv .venv
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
```

Лаунчер `py.exe` тоже не в PATH; его полный путь —
`C:\Users\User\AppData\Local\Programs\Python\Launcher\py.exe`,
интерпретатор — Python 3.13.15.
