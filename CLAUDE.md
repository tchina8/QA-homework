# CLAUDE.md

## Python: только из .venv по прямому пути

**Правило (действует и для субагентов, и для новых сессий):**
все команды `python` и `pytest` вызывать **только из `.venv` по прямому пути**.

Состояние PATH в этом окружении менялось и доверять ему нельзя. Изначально
`python` в PATH был заглушкой-алиасом Microsoft Store, а `py` и `pytest`
отсутствовали вовсе. Проверка от 28 августа 2026 показала обратное: `py`,
`python` и `pytest` из PATH вызываются и дают Python 3.13.15.

Это **не отменяет правило, а усиливает его**. Голый `pytest` теперь берётся
из `C:\Users\User\AppData\Local\Programs\Python\Python313\Scripts\pytest.exe`,
то есть из глобального окружения, а не из проектного. На этом проекте он
проходит — и потому особенно опасен: неверная команда молча делает вид,
что всё в порядке, хотя проверяет другой интерпретатор с другим набором
зависимостей.

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

Запрещено: голые `python`, `py`, `pytest`, `pip` — даже когда они работают;
`activate`-скрипты (состояние оболочки между вызовами инструментов
не сохраняется); правка PATH, в том числе подстановка путей из реестра.

## Пересоздание окружения

Венв (`.venv/`) в git не хранится — он в `.gitignore`. Состав зависимостей —
в `requirements.txt`.

```powershell
& "C:\Users\User\AppData\Local\Programs\Python\Launcher\py.exe" -3.13 -m venv .venv
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
```

Полный путь к лаунчеру на случай, если `py` снова пропадёт из PATH —
`C:\Users\User\AppData\Local\Programs\Python\Launcher\py.exe`,
интерпретатор — Python 3.13.15.
