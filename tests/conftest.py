"""Общие фикстуры и хелперы регресса кредитного калькулятора.

Источник ожиданий — `docs/spec.md`, и только он. Эталонные таблицы разбираются
из самого документа, а не переписываются руками в код теста: если спека изменится,
тесты обязаны разойтись с реализацией, а не молча продолжать проверять старое.

Здесь нет ни одного ожидания «по факту работы кода». Всё, что этот модуль умеет:
разобрать спеку, дать параметры кредита и сравнить деньги до копейки.
"""

from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence

import pytest

# Корень репозитория в sys.path, иначе `import calc` из tests/ не находится:
# pytest кладёт в путь каталог с conftest, а не родительский.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SPEC_PATH = REPO_ROOT / "docs" / "spec.md"

#: Денежный ноль. Строка графика обязана иметь ровно 2 знака — инвариант И-8.
ZERO = Decimal("0.00")

#: Заголовки колонок эталонных таблиц спеки -> имена полей строки графика (раздел 4.3).
_COLUMNS = {
    "k": "number",
    "Платёж": "payment",
    "Проценты": "interest",
    "Тело": "principal",
    "Досрочка": "prepayment",
    "Остаток": "balance",
}


class SpecRow(NamedTuple):
    """Строка эталонного графика, разобранная из таблицы спеки.

    Поля повторяют раздел 4.3 спеки. `prepayment` равен `0.00`, если колонки
    «Досрочка» в таблице не было: спека оговаривает, что в таблицах 6.1 и 6.3
    она опущена как пустая, но форма строки от этого не меняется.
    """

    number: int
    payment: Decimal
    interest: Decimal
    principal: Decimal
    prepayment: Decimal
    balance: Decimal


class ThresholdRow(NamedTuple):
    """Строка таблицы порогов из раздела 4.2 спеки (находка Н-3).

    `prepayment` равен `None` для строки «без досрочки»; `saved` равен `None`
    там, где спека экономию не приводит.
    """

    prepayment: Decimal | None
    months: int
    last_payment: Decimal
    saved: Decimal | None


def to_decimal(text: str) -> Decimal:
    """Превратить число из спеки в `Decimal`.

    Вход: ячейка таблицы вида `"88 848.79"`, `"**1 066 185.45**"`, `"0.01"`.
    Выход: точный `Decimal`.

    Пробелы-разделители разрядов и жирное выделение убираются; неразрывный пробел
    обрабатывается наравне с обычным.
    """
    cleaned = text.replace("**", "")
    cleaned = re.sub(r"[\s  ]", "", cleaned)
    return Decimal(cleaned)


def kopecks(value: Decimal | str) -> Decimal:
    """Построить денежную величину из строки, не округляя.

    Вход: строка `"1000000.00"` либо уже готовый `Decimal`.
    Выход: `Decimal`.

    Округления здесь нет намеренно: тест не имеет права приводить ожидание
    к копейкам — это работа проверяемого кода (раздел 1.1 спеки).
    """
    return value if isinstance(value, Decimal) else Decimal(value)


def assert_money_equal(actual, expected, label: str = "сумма") -> None:
    """Сравнить две денежные величины точно, до копейки.

    Вход: фактическое и ожидаемое значения, подпись для сообщения.
    Выход: `None`; при расхождении — `AssertionError` с обеими величинами и разницей.

    Сравнение строгое, без допуска: раздел 1 спеки требует точного равенства
    `Decimal`, а не приблизительного. Дополнительно проверяется масштаб —
    ровно 2 знака после запятой, инвариант И-8.
    """
    assert isinstance(actual, Decimal), f"{label}: ожидается Decimal, получен {type(actual).__name__}"
    expected = kopecks(expected)
    assert actual == expected, (
        f"{label}: получено {actual}, спека требует {expected}, разница {actual - expected}"
    )
    assert -actual.as_tuple().exponent == 2, (
        f"{label}: {actual} записано не с 2 знаками после запятой (инвариант И-8)"
    )


def assert_schedule_equal(actual_rows: Sequence, expected_rows: Sequence[SpecRow]) -> None:
    """Сверить построенный график с эталонной таблицей спеки построчно.

    Вход: график из `calc` и разобранные строки спеки.
    Выход: `None`; при расхождении — `AssertionError` с номером месяца и полем.

    Сначала сравнивается число строк: если оно разошлось, сверять суммы бессмысленно,
    и сообщение об этом полезнее, чем каскад ошибок по каждому месяцу.
    """
    assert len(actual_rows) == len(expected_rows), (
        f"число строк графика: получено {len(actual_rows)}, "
        f"спека требует {len(expected_rows)}"
    )
    for actual, expected in zip(actual_rows, expected_rows):
        assert actual.number == expected.number, (
            f"номер месяца: получено {actual.number}, спека требует {expected.number}"
        )
        k = expected.number
        assert_money_equal(actual.payment, expected.payment, f"месяц {k}, платёж")
        assert_money_equal(actual.interest, expected.interest, f"месяц {k}, проценты")
        assert_money_equal(actual.principal, expected.principal, f"месяц {k}, тело")
        assert_money_equal(actual.prepayment, expected.prepayment, f"месяц {k}, досрочка")
        assert_money_equal(actual.balance, expected.balance, f"месяц {k}, остаток")


def sum_money(values: Iterable[Decimal]) -> Decimal:
    """Сложить денежные величины без округления.

    Вход: последовательность `Decimal`.
    Выход: точная сумма; для пустой последовательности — `0.00`.

    Нужно инвариантам И-1 и И-9, где спека требует точного равенства сумм.
    """
    return sum(values, ZERO)


def _table_after(spec_text: str, anchor: str) -> list[list[str]]:
    """Вернуть первую markdown-таблицу, идущую после якорной строки.

    Вход: текст спеки и подстрока-якорь (заголовок раздела или выделенный подзаголовок).
    Выход: список строк таблицы, каждая — список ячеек; первая строка — заголовки.

    Строки с многоточием пропускаются: спека сокращает длинные таблицы строкой `…`.
    """
    start = spec_text.index(anchor)
    rows: list[list[str]] = []
    for line in spec_text[start:].splitlines()[1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        if any("…" in cell for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        raise AssertionError(f"в docs/spec.md после «{anchor}» не найдено таблицы")
    return rows


def _schedule_from_table(spec_text: str, anchor: str) -> list[SpecRow]:
    """Разобрать эталонный график из таблицы спеки.

    Вход: текст спеки и якорь нужной таблицы.
    Выход: список `SpecRow`.

    Колонки определяются по заголовку, а не по позиции: таблицы 6.1 и 6.3 идут
    без «Досрочки», таблицы 6.2 — с ней.
    """
    header, *body = _table_after(spec_text, anchor)
    fields = [_COLUMNS[title.replace("**", "").strip()] for title in header]
    schedule: list[SpecRow] = []
    for cells in body:
        values: dict[str, object] = {"prepayment": ZERO}
        for field, cell in zip(fields, cells):
            if field == "number":
                values[field] = int(to_decimal(cell))
            elif cell.replace("**", "").strip() in {"—", "-", ""}:
                values[field] = ZERO
            else:
                values[field] = to_decimal(cell)
        schedule.append(SpecRow(**values))  # type: ignore[arg-type]
    return schedule


# --------------------------------------------------------------------------- фикстуры


@pytest.fixture(scope="session")
def spec_text() -> str:
    """Полный текст `docs/spec.md`.

    Вход: нет. Выход: строка.
    Нужен тестам, которые сверяются с формулировками спеки, а не только с числами.
    """
    return SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def base_loan() -> dict:
    """Параметры кредита эталонного примера 6.1 спеки.

    Вход: нет.
    Выход: словарь `principal` / `annual_rate` / `months` — `1 000 000.00`, 12 %, 12 месяцев.

    Этот же кредит используют примеры 6.2 (досрочка) и 6.3a (страховка
    из своих средств), поэтому параметры вынесены в одно место.
    """
    return {
        "principal": Decimal("1000000.00"),
        "annual_rate": Decimal("0.12"),
        "months": 12,
    }


@pytest.fixture(scope="session")
def ref_base_schedule(spec_text: str) -> list[SpecRow]:
    """Эталонный график примера 6.1 — базовый аннуитет без досрочек и страховки.

    Вход: текст спеки. Выход: 12 строк `SpecRow`.
    Раздел 6.1 спеки.
    """
    return _schedule_from_table(spec_text, "### 6.1.")


@pytest.fixture(scope="session")
def ref_term_reduction_schedule(spec_text: str) -> list[SpecRow]:
    """Эталонный график примера 6.2a — досрочка `100 000.00` с сокращением срока.

    Вход: текст спеки. Выход: 11 строк `SpecRow`.
    Раздел 6.2 спеки, вариант «2a. Сокращение срока».
    """
    return _schedule_from_table(spec_text, "**2a. Сокращение срока**")


@pytest.fixture(scope="session")
def ref_payment_reduction_schedule(spec_text: str) -> list[SpecRow]:
    """Эталонный график примера 6.2b — досрочка `100 000.00` с уменьшением платежа.

    Вход: текст спеки. Выход: 12 строк `SpecRow`.
    Раздел 6.2 спеки, вариант «2b. Уменьшение платежа». Содержит находку Н-1:
    последний платёж больше регулярного, хвоста в копейку быть не должно.
    """
    return _schedule_from_table(spec_text, "**2b. Уменьшение платежа**")


@pytest.fixture(scope="session")
def ref_financed_insurance_schedule(spec_text: str) -> list[SpecRow]:
    """Эталонный график примера 6.3b — страховка в кредит, тело `1 050 000.00`.

    Вход: текст спеки. Выход: 12 строк `SpecRow`.
    Раздел 6.3 спеки, вариант «3b. В кредит».
    """
    return _schedule_from_table(spec_text, "**3b. В кредит**")


@pytest.fixture(scope="session")
def ref_equal_interest_schedule(spec_text: str) -> list[SpecRow]:
    """Эталонный график находки Н-2 — совпадение соседних процентов.

    Вход: текст спеки. Выход: строки `SpecRow` из таблицы раздела 4.1.

    Кредит `100.00 / 0.1 % / 12 мес`. Спека сокращает таблицу строкой `…`,
    поэтому строк меньше двенадцати: сверять по ним можно только приведённые
    месяцы, но не длину графика целиком.
    """
    return _schedule_from_table(spec_text, "### 4.1.")


@pytest.fixture(scope="session")
def ref_prepayment_thresholds(spec_text: str) -> list[ThresholdRow]:
    """Таблица порогов досрочки из раздела 4.2 спеки — находка Н-3.

    Вход: текст спеки.
    Выход: список `ThresholdRow` с досрочкой, числом месяцев, последним платежом
    и экономией процентов.

    Показывает, что копеечная досрочка срок не сокращает, а на пороге `81 237.96`
    последний платёж равен `0.01`.
    """
    header, *body = _table_after(spec_text, "### 4.2.")
    assert header[0].startswith("Досрочка"), (
        f"раздел 4.2 спеки: неожиданный заголовок таблицы порогов — {header}"
    )
    thresholds: list[ThresholdRow] = []
    for cells in body:
        raw_prepayment, raw_months, raw_last, raw_saved = cells
        thresholds.append(
            ThresholdRow(
                prepayment=None if "—" in raw_prepayment else to_decimal(raw_prepayment),
                months=int(to_decimal(raw_months)),
                last_payment=to_decimal(raw_last),
                saved=None if "—" in raw_saved else to_decimal(raw_saved),
            )
        )
    return thresholds
