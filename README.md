# Регресс кредитного калькулятора

Домашнее задание №2, **вариант B4** — регресс кредитного калькулятора: ставка,
срок, досрочное погашение, страховка.

В репозитории лежит спецификация как источник истины, написанный по ней
калькулятор, 266 автоматических проверок и повторяемая процедура прогона
с независимым контролем результата.

**Главный результат работы — отрицательный, и он же главный вывод.** Прогон
показывает `265 passed, 1 xfailed`, и это состояние не изменилось после того,
как в калькуляторе нашли **ещё десять дефектов, и набор не ловит ни одного
из них**: отдельная сессия агента-скептика прогнала 20 сценариев мимо
существующих проверок и сломала расчёт в десяти из них (BUG-11 … BUG-20).
Самый крупный — `schedule_with_term_reduction` падает исключением **без единой
досрочки, на 46 % потребительских конфигураций**, начиная со срока в пять
месяцев. Причина слепой зоны не в сложности кода:
параметризация тестов идёт по видам досрочки и схемам страхования, но кредит
во всех проверках один и тот же — `1 000 000.00 / 12 % / 12 мес`. Четыре находки
ловятся уже написанными ассертами, стоит добавить в параметры второй кредит.
Разбор — в [`findings.md`](findings.md) и в [`REPORT.md`](REPORT.md).

**`1 xfailed` в прогоне — это закреплённый дефект BUG-01, а не поломка набора.**
Тест описывает поведение, которого требует спецификация, ожидания не ослаблены
под факт, режим строгий: когда дефект починят, прогон упадёт с `XPASS` и молча
закрыть его не выйдет.

---

## Запуск с нуля

Нужен Python 3.13 и git. Всё остальное ставится ниже.

### 1. Клонировать

```bash
git clone https://github.com/tchina8/QA-homework.git
cd QA-homework
```

### 2. Создать виртуальное окружение

Виртуальное окружение в git не хранится — его нужно создать. Ниже команды
для Windows PowerShell; если `py` не виден в PATH, укажите полный путь
к лаунчеру.

```powershell
py -3.13 -m venv .venv
```

Если `py` не найден, на Windows он обычно лежит здесь:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Launcher\py.exe" -3.13 -m venv .venv
```

На Linux и macOS:

```bash
python3.13 -m venv .venv
```

### 3. Поставить зависимости

```powershell
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
```

На Linux и macOS путь к интерпретатору другой — `.venv/bin/python`:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Для тестов больше ничего не нужно: в `requirements.txt` только `pytest`.

### 4. Прогнать тесты

```powershell
& ".venv\Scripts\pytest.exe" -v
```

```bash
.venv/bin/pytest -v
```

Ожидаемый результат:

```
265 passed, 1 xfailed
```

Один `xfail` — не сбой. Это тест, закрепляющий незакрытый дефект **BUG-01**;
он помечен строгим `xfail`, поэтому при починке дефекта прогон сразу упадёт
с `XPASS`, и закрыть баг молча не выйдет.

> **Про вызов Python в этом проекте.** Интерпретатор и `pytest` вызываются
> **только из `.venv` по прямому пути**. В окружении, где работа велась,
> `python` в PATH — это заглушка Microsoft Store, а `py` и `pytest` отсутствуют;
> правило зафиксировано в [`CLAUDE.md`](CLAUDE.md), чтобы действовало
> и в новых сессиях агента.

---

## Как вызвать workflow

Повторяемая процедура регрессионного прогона: шесть шагов, каждый оставляет
файл-артефакт. Проверку результата выполняет **отдельный агент**, не тот,
который прогонял.

Руками:

```powershell
& ".venv\Scripts\python.exe" workflow\run_regression.py step1
# шаг 1 печатает последней строкой STAMP=ГГГГ-ММ-ДД_ЧЧММСС
& ".venv\Scripts\python.exe" workflow\run_regression.py step2 --stamp <STAMP>
& ".venv\Scripts\python.exe" workflow\run_regression.py step3 --stamp <STAMP>
& ".venv\Scripts\python.exe" workflow\run_regression.py step4 --stamp <STAMP>
& ".venv\Scripts\python.exe" workflow\run_regression.py step5 --stamp <STAMP>
```

Шаг 6 — вердикт проверяющего агента. Готовые задания для обоих агентов лежат
в [`workflow/regression-run.md`](workflow/regression-run.md): одно копируется
исполнителю, второе — проверяющему.

Подробности, выходные файлы и известные слабости самой процедуры —
в [`workflow/README.md`](workflow/README.md).

### Пересборка PDF-отчёта

Нужна отдельно и только для отчёта. Тянет `playwright` и `pypdfium2`;
свой браузер `playwright` не скачивает — управляет уже установленным Chrome.

```powershell
& ".venv\Scripts\python.exe" -m pip install -r requirements-docs.txt
& ".venv\Scripts\python.exe" workflow\build_pdf.py reports\regression-report.html
```

---

## Структура

```
docs/spec.md                  Спецификация — источник истины для всех тестов
calc/                         Объект тестирования
  money.py                    Decimal из строк, единственная функция округления
  annuity.py                  Аннуитетный платёж и график погашения
  prepayment.py               Досрочка: сокращение срока и уменьшение платежа
  insurance.py                Страховка: из своих средств и в кредит
tests/                        Регресс, 266 проверок
  conftest.py                 Фикстуры; эталоны разбираются из docs/spec.md
  test_annuity.py             Ставка, срок, график, инварианты
  test_prepayment.py          Досрочка обоих видов
  test_insurance.py           Страховка обеих схем
  test_rounding_discipline.py Округление только в money.py, запрет float
workflow/                     Повторяемая процедура прогона
  regression-run.md           Задания исполнителю и проверяющему
  run_regression.py           Детерминированные шаги 1-5
  build_pdf.py                Сборка PDF из HTML
  README.md                   Что делает, как вызывать, что на выходе
reports/                      Результаты прогонов
  regression-report.pdf       Отчёт для чтения
  raw/                        Сырой вывод pytest по прогонам
  steps/                      Артефакты шагов, по файлу на шаг
sessions/01-agents.md         Журнал запуска трёх параллельных агентов
findings.md                   Дефекты: BUG-01 ... BUG-10
REPORT.md                     Отчёт по трём обязательным пунктам задания
```

## Где лежит PDF-отчёт

**[`reports/regression-report.pdf`](reports/regression-report.pdf)** — 10 страниц:
резюме, таблица прогона, находки, слепая зона регресса, что не сработало
и что осталось. Исходник вёрстки — `reports/regression-report.html`.

## Что читать по порядку

| Файл | О чём |
|---|---|
| [`REPORT.md`](REPORT.md) | Отчёт по трём обязательным пунктам задания и что не сработало |
| [`reports/regression-report.pdf`](reports/regression-report.pdf) | То же самое, свёрстанное для чтения |
| [`findings.md`](findings.md) | Дефекты с денежными последствиями на длинном сроке |
| [`docs/spec.md`](docs/spec.md) | Спецификация: формулы, инварианты, эталонные примеры |
| [`sessions/01-agents.md`](sessions/01-agents.md) | Журнал работы трёх параллельных агентов |
