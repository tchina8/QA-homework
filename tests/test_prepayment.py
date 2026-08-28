"""Регресс досрочного погашения — оба вида: сокращение срока и уменьшение платежа.

Источник ожиданий — `docs/spec.md`, и только он: разделы 3.4, 3.6, 4 (инварианты),
4.2 (находка Н-3), 5 (вырожденные случаи), 6.2 (эталонные примеры 2a и 2b),
7 (обязательные кейсы Н-1 и Н-3). Ни одно число здесь не получено запуском
реализации: эталонные графики и таблица порогов разбираются из самой спеки
фикстурами `conftest`, итоги — из кодовых блоков спеки, отдельные величины
процитированы из текста раздела с указанием пункта.

Два места требуют пояснения, потому что «очевидное» ожидание здесь ложно и спека
предупреждает об этом прямым текстом:

* **Н-1 (разделы 3.4 и 6.2b).** При уменьшении платежа последний платёж
  `77 174.76` **больше** регулярного `77 174.75`. Тест «последний платёж всегда
  меньше регулярного» упал бы ложно; правильное ожидание — И-3, равенство
  `money(B_11 + I_12)`. Хвоста в копейку и тринадцатой строки быть не должно.
* **Н-3 (раздел 4.2).** Копеечная досрочка срок не сокращает, а на пороге
  `81 237.96` последний платёж равен `0.01`. И-10 нестрогий: срок сокращается
  на 0 или больше месяцев, требовать строгого `< n` нельзя.

Инвариант И-3 намеренно **не** входит в общий набор проверок `assert_invariants`:
когда кредит закрывается самой досрочкой (раздел 5, «досрочка ровно в остаток»),
последняя строка несёт обычный регулярный платёж, а не балансирующий, и равенство
`A_last = money(B_{last−1} + I_last)` там не выполняется. И-3 проверяется отдельно
там, где график действительно закрывается балансировкой.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

import pytest

from conftest import (
    ZERO,
    SpecRow,
    assert_money_equal,
    assert_schedule_equal,
    sum_money,
    to_decimal,
)

from calc.annuity import ScheduleDidNotTerminate, total_interest, total_paid
from calc.prepayment import (
    interest_saved,
    schedule_with_payment_reduction,
    schedule_with_term_reduction,
)
from calc.prepayment import term_reduction as term_reduction_of

#: Месяц внесения досрочки во всех эталонных примерах 6.2 и в таблице порогов 4.2.
PREPAYMENT_MONTH = 3

#: Оба вида досрочного погашения (раздел 3.6) — для параметризации общих проверок.
#: Ключ — название вида по-русски, оно же попадает в сообщения об ошибках.
BUILDERS = {
    "сокращение срока": schedule_with_term_reduction,
    "уменьшение платежа": schedule_with_payment_reduction,
}

#: Идентификаторы кейсов для pytest: латиницей, иначе `-k` и отчёт нечитаемы.
BUILDER_IDS = ["term_reduction", "payment_reduction"]


# ------------------------------------------------------------------ вспомогательное


def spec_money(value: Decimal) -> Decimal:
    """Округлить по правилу раздела 1.1 спеки, **не** обращаясь к `calc`.

    Вход: точный `Decimal`. Выход: `Decimal` с 2 знаками, `ROUND_HALF_UP`.

    Реализовано в тесте отдельно от `calc/money.py` намеренно: ожидание, собранное
    вызовом проверяемой функции округления, проверяло бы код против самого себя.
    Формула выписана из блока кода раздела 1.1 спеки дословно.
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def monthly_rate(annual_rate: Decimal) -> Decimal:
    """Месячная ставка `i = r / 12` (раздел 3.1 спеки), без округления.

    Вход: годовая ставка. Выход: точное частное `Decimal`.
    """
    return annual_rate / 12


def spec_summary(spec_text: str, anchor: str) -> dict[str, Decimal]:
    """Разобрать итоговый блок спеки, идущий после якорного подзаголовка.

    Вход: текст спеки и якорь (например, `"**2a. Сокращение срока**"`).
    Выход: словарь «подпись → число», например `{"Всего выплачено": Decimal("1057020.10")}`.

    Итоги примеров 6.2 лежат не в таблице, а в блоке кода под ней, поэтому фикстуры
    `conftest` их не отдают. Как и таблицы, они читаются из документа, а не
    переписываются в тест руками.
    """
    start = spec_text.index(anchor)
    opening = spec_text.index("```", start)
    closing = spec_text.index("```", opening + 3)
    summary: dict[str, Decimal] = {}
    for line in spec_text[opening + 3 : closing].strip().splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) != 2:
            continue
        summary[parts[0]] = to_decimal(parts[1])
    assert summary, f"в docs/spec.md после «{anchor}» не найдено блока с итогами"
    return summary


def build(kind: str, loan: dict, prepayments: dict[int, Decimal]) -> list:
    """Построить график нужного вида досрочки.

    Вход: название вида из `BUILDERS`, параметры кредита, досрочки.
    Выход: список строк графика.
    """
    return BUILDERS[kind](
        loan["principal"], loan["annual_rate"], loan["months"], prepayments
    )


def assert_invariants(rows, principal: Decimal, months: int, label: str) -> None:
    """Проверить инварианты раздела 4 спеки на графике с досрочками.

    Вход: график, тело кредита `S`, исходный срок `n`, подпись для сообщений.
    Выход: `None`; при нарушении — `AssertionError` с номером инварианта.

    Проверяются И-1, И-2, И-4, И-5, И-6, И-7, И-8, И-9, И-10. И-3 сюда не входит
    сознательно: см. модульный docstring — при закрытии кредита досрочкой последняя
    строка несёт регулярный платёж, а не балансирующий.
    """
    assert rows, f"{label}: график пуст, а пустой график спека допустимым не считает"

    # И-10: число строк не превышает исходный срок; сокращение на 0 месяцев законно.
    assert len(rows) <= months, (
        f"{label}: строк {len(rows)}, исходный срок {months} — И-10 запрещает рост срока"
    )

    # И-8 и И-7: ровно два знака и неотрицательность каждой денежной величины.
    for row in rows:
        for field in ("payment", "interest", "principal", "prepayment", "balance"):
            value = getattr(row, field)
            assert isinstance(value, Decimal), (
                f"{label}, месяц {row.number}, {field}: ожидается Decimal, "
                f"получен {type(value).__name__}"
            )
            assert -value.as_tuple().exponent == 2, (
                f"{label}, месяц {row.number}, {field} = {value}: не 2 знака (И-8)"
            )
            assert value >= 0, (
                f"{label}, месяц {row.number}, {field} = {value}: отрицательно (И-7)"
            )

    # Номера месяцев идут подряд с единицы — раздел 2, `k = 1 … n`.
    assert [row.number for row in rows] == list(range(1, len(rows) + 1)), (
        f"{label}: номера месяцев {[row.number for row in rows]} идут не подряд с 1"
    )

    # И-1: сумма тел вместе с досрочками равна телу кредита, точно.
    bodies = sum_money(row.principal + row.prepayment for row in rows)
    assert bodies == principal, (
        f"{label}: Σ(D_k + E_k) = {bodies}, тело кредита {principal}, "
        f"разница {bodies - principal} — нарушен И-1"
    )

    # И-9: сумма выплат сходится с телом и процентами.
    paid = sum_money(row.payment for row in rows) + sum_money(row.prepayment for row in rows)
    interest = sum_money(row.interest for row in rows)
    assert paid == principal + interest, (
        f"{label}: ΣA + ΣE = {paid}, S + ΣI = {principal + interest}, "
        f"разница {paid - principal - interest} — нарушен И-9"
    )

    # И-2: остаток после последнего платежа строго ноль, без допуска.
    assert rows[-1].balance == ZERO, (
        f"{label}: остаток после последнего платежа {rows[-1].balance}, требуется 0.00 (И-2)"
    )

    previous_balance = principal
    for row in rows:
        # И-6: платёж покрывает проценты, тело регулярного платежа положительно.
        assert row.payment > row.interest, (
            f"{label}, месяц {row.number}: платёж {row.payment} не превышает "
            f"проценты {row.interest} — нарушен И-6"
        )
        assert row.principal > 0, (
            f"{label}, месяц {row.number}: тело платежа {row.principal} не положительно (И-6)"
        )
        # И-5: остаток убывает строго.
        assert row.balance < previous_balance, (
            f"{label}, месяц {row.number}: остаток {row.balance} не меньше "
            f"предыдущего {previous_balance} — нарушен И-5"
        )
        # Раздел 3.3: B_k = B_{k−1} − D_k − E_k.
        expected_balance = previous_balance - row.principal - row.prepayment
        assert row.balance == expected_balance, (
            f"{label}, месяц {row.number}: остаток {row.balance}, "
            f"а B_{{k−1}} − D_k − E_k = {expected_balance} (раздел 3.3)"
        )
        previous_balance = row.balance

    # И-4: проценты не возрастают. Неравенство нестрогое — раздел 4.1, находка Н-2.
    for previous, current in zip(rows, rows[1:]):
        assert previous.interest >= current.interest, (
            f"{label}: проценты выросли с {previous.interest} (месяц {previous.number}) "
            f"до {current.interest} (месяц {current.number}) — нарушен И-4"
        )


# ------------------------------------------------------ 6.2a. Сокращение срока


def test_term_reduction_matches_reference_schedule(
    base_loan: dict, ref_term_reduction_schedule: list[SpecRow]
) -> None:
    """График сокращения срока совпадает с эталонной таблицей 6.2a построчно.

    Раздел 6.2 спеки, вариант «2a». Досрочка `100 000.00` с платежом №3, платёж
    остаётся `88 848.79`, срок сжимается до 11 месяцев. Сверяются все шесть полей
    каждой из 11 строк (раздел 4.3), включая колонку досрочки.
    """
    schedule = schedule_with_term_reduction(
        base_loan["principal"],
        base_loan["annual_rate"],
        base_loan["months"],
        {PREPAYMENT_MONTH: Decimal("100000.00")},
    )
    assert_schedule_equal(schedule, ref_term_reduction_schedule)


def test_term_reduction_totals_and_savings(
    base_loan: dict, spec_text: str, ref_term_reduction_schedule: list[SpecRow]
) -> None:
    """Итоги примера 6.2a: выплачено, проценты, срок, экономия.

    Раздел 6.2 спеки. «Всего выплачено» в спеке — это Σ платежей вместе с досрочкой
    (`957 020.10 + 100 000.00`), поэтому `total_paid` обязан учитывать досрочку:
    иначе не сойдётся И-9 (`ΣA + ΣE = S + ΣI`).
    """
    summary = spec_summary(spec_text, "**2a. Сокращение срока**")
    base = schedule_with_term_reduction(
        base_loan["principal"], base_loan["annual_rate"], base_loan["months"], {}
    )
    schedule = schedule_with_term_reduction(
        base_loan["principal"],
        base_loan["annual_rate"],
        base_loan["months"],
        {PREPAYMENT_MONTH: Decimal("100000.00")},
    )

    assert len(schedule) == int(summary["Месяцев"]) == len(ref_term_reduction_schedule), (
        f"месяцев в графике {len(schedule)}, спека требует {int(summary['Месяцев'])}"
    )
    assert_money_equal(total_paid(schedule), summary["Всего выплачено"], "всего выплачено, 6.2a")
    assert_money_equal(total_interest(schedule), summary["Проценты"], "проценты, 6.2a")
    assert_money_equal(
        interest_saved(base, schedule), summary["Экономия процентов"], "экономия процентов, 6.2a"
    )
    assert term_reduction_of(base, schedule) == base_loan["months"] - int(summary["Месяцев"]), (
        f"сокращение срока: получено {term_reduction_of(base, schedule)}, "
        f"спека требует {base_loan['months'] - int(summary['Месяцев'])} месяц(ев)"
    )


def test_term_reduction_has_exactly_one_balancing_row(
    base_loan: dict, ref_term_reduction_schedule: list[SpecRow]
) -> None:
    """И-3: в графике 6.2a ровно одна строка отличается от регулярного платежа.

    Раздел 4 спеки, инвариант И-3: «ровно одна» строка вправе иметь платёж,
    отличный от `A`, и он равен `money(B_{last−1} + I_last)`. Тест, допускающий
    две такие строки, пропустит дефект округления.
    """
    schedule = schedule_with_term_reduction(
        base_loan["principal"],
        base_loan["annual_rate"],
        base_loan["months"],
        {PREPAYMENT_MONTH: Decimal("100000.00")},
    )
    regular = ref_term_reduction_schedule[0].payment
    deviating = [row.number for row in schedule if row.payment != regular]
    assert deviating == [len(schedule)], (
        f"от регулярного платежа {regular} отличаются месяцы {deviating}, "
        f"И-3 разрешает ровно один — последний ({len(schedule)})"
    )
    last, previous = schedule[-1], schedule[-2]
    assert_money_equal(
        last.payment,
        spec_money(previous.balance + last.interest),
        "последний платёж 6.2a, money(B_{last−1} + I_last)",
    )


# ------------------------------------------------------ 6.2b. Уменьшение платежа


def test_payment_reduction_matches_reference_schedule(
    base_loan: dict, ref_payment_reduction_schedule: list[SpecRow]
) -> None:
    """График уменьшения платежа совпадает с эталонной таблицей 6.2b построчно.

    Раздел 6.2 спеки, вариант «2b». Срок остаётся 12 месяцев, с месяца 4 платёж
    пересчитан на остаток `661 080.28` и 9 оставшихся месяцев: `A' = 77 174.75`.
    Сверяются все шесть полей всех 12 строк.
    """
    schedule = schedule_with_payment_reduction(
        base_loan["principal"],
        base_loan["annual_rate"],
        base_loan["months"],
        {PREPAYMENT_MONTH: Decimal("100000.00")},
    )
    assert_schedule_equal(schedule, ref_payment_reduction_schedule)


def test_payment_reduction_totals_and_savings(
    base_loan: dict, spec_text: str
) -> None:
    """Итоги примера 6.2b: выплачено `1 061 119.13`, проценты `61 119.13`, экономия `5 066.32`.

    Раздел 6.2 спеки. Срок не сокращается вовсе — `term_reduction` обязан вернуть `0`,
    и это законный результат по И-10, а не признак дефекта.
    """
    summary = spec_summary(spec_text, "**2b. Уменьшение платежа**")
    base = schedule_with_payment_reduction(
        base_loan["principal"], base_loan["annual_rate"], base_loan["months"], {}
    )
    schedule = schedule_with_payment_reduction(
        base_loan["principal"],
        base_loan["annual_rate"],
        base_loan["months"],
        {PREPAYMENT_MONTH: Decimal("100000.00")},
    )

    assert len(schedule) == int(summary["Месяцев"]), (
        f"месяцев в графике {len(schedule)}, спека требует {int(summary['Месяцев'])}"
    )
    assert_money_equal(total_paid(schedule), summary["Всего выплачено"], "всего выплачено, 6.2b")
    assert_money_equal(total_interest(schedule), summary["Проценты"], "проценты, 6.2b")
    assert_money_equal(
        interest_saved(base, schedule), summary["Экономия процентов"], "экономия процентов, 6.2b"
    )
    assert term_reduction_of(base, schedule) == 0, (
        f"сокращение срока при уменьшении платежа: получено "
        f"{term_reduction_of(base, schedule)}, спека требует 0 — срок фиксирован"
    )


def test_payment_reduction_last_payment_is_bigger_than_regular_one(
    base_loan: dict, ref_payment_reduction_schedule: list[SpecRow]
) -> None:
    """Находка Н-1: последний платёж 6.2b больше регулярного, тринадцатой строки нет.

    Разделы 3.4, 6.2b и 7 (кейс Н-1). Без условия 3.4(б) «`k = n`» после 12-го платежа
    остаётся хвост `0.01` и появляется фиктивный 13-й платёж на копейку. Здесь
    проверяется именно это: строк ровно 12, последний платёж `77 174.76` — на копейку
    **больше** регулярного `77 174.75`, и он равен `money(B_11 + I_12)` по И-3.

    Тест «последний платёж всегда меньше регулярного» на этих данных упал бы ложно:
    спека предупреждает об этом прямым текстом, знак балансировки здесь
    противоположен примеру 6.1.
    """
    schedule = schedule_with_payment_reduction(
        base_loan["principal"],
        base_loan["annual_rate"],
        base_loan["months"],
        {PREPAYMENT_MONTH: Decimal("100000.00")},
    )
    expected_last = ref_payment_reduction_schedule[-1]
    regular = ref_payment_reduction_schedule[-2].payment

    assert len(schedule) == base_loan["months"], (
        f"строк в графике {len(schedule)}, спека требует ровно {base_loan['months']}: "
        f"тринадцатая строка на копейку — дефект из находки Н-1 (раздел 3.4)"
    )
    assert not any(row.payment == Decimal("0.01") for row in schedule), (
        "в графике есть платёж на копейку — это хвост из находки Н-1, "
        "закрываемый условием 3.4(б)"
    )
    assert_money_equal(schedule[-1].payment, expected_last.payment, "последний платёж 6.2b")
    assert schedule[-1].payment > regular, (
        f"последний платёж {schedule[-1].payment} обязан быть БОЛЬШЕ регулярного "
        f"{regular} (раздел 6.2b: {expected_last.payment} против {regular})"
    )
    assert_money_equal(
        schedule[-1].payment,
        spec_money(schedule[-2].balance + schedule[-1].interest),
        "последний платёж 6.2b, money(B_11 + I_12)",
    )
    assert_money_equal(schedule[-1].balance, ZERO, "остаток после 12-го платежа")


def test_term_reduction_saves_more_than_payment_reduction(
    base_loan: dict, spec_text: str
) -> None:
    """Сокращение срока выгоднее уменьшения платежа ровно на `4 099.03`.

    Раздел 6.2 спеки: «Сравнение видов при прочих равных» — `9 165.35` против
    `5 066.32`. Проверяется именно разница: каждый график по отдельности может
    сойтись, а сравнение видов — разойтись.
    """
    match = re.search(r"Разница `([\d\s ]+\.\d{2})`", spec_text)
    assert match, "в docs/spec.md не найдено формулировки «Разница ... в пользу сокращения срока»"
    expected_difference = to_decimal(match.group(1))

    prepayments = {PREPAYMENT_MONTH: Decimal("100000.00")}
    args = (base_loan["principal"], base_loan["annual_rate"], base_loan["months"])
    saved_by_term = interest_saved(
        schedule_with_term_reduction(*args, {}), schedule_with_term_reduction(*args, prepayments)
    )
    saved_by_payment = interest_saved(
        schedule_with_payment_reduction(*args, {}),
        schedule_with_payment_reduction(*args, prepayments),
    )
    assert saved_by_term > saved_by_payment, (
        f"сокращение срока сэкономило {saved_by_term}, уменьшение платежа "
        f"{saved_by_payment}: спека требует выгоды в пользу сокращения срока"
    )
    assert_money_equal(
        saved_by_term - saved_by_payment, expected_difference, "разница экономий 6.2a и 6.2b"
    )


# ------------------------------------------------ раздел 3.6: порядок списания досрочки


@pytest.mark.parametrize(
    "prepayment",
    ["0.01", "1.00", "100000.00", "500000.00"],
    ids=lambda text: f"E3={text}",
)
def test_interest_of_prepayment_month_ignores_the_prepayment(
    base_loan: dict, ref_base_schedule: list[SpecRow], prepayment: str
) -> None:
    """Проценты месяца досрочки начислены на остаток ДО досрочки.

    Раздел 3.6 спеки: `E_k` вносится вместе с платежом `k` и списывается ПОСЛЕ
    разнесения платежа на проценты и тело. Раздел 8.2 называет тест на порядок
    обязательным: если реализация спишет `E_k` раньше, числа примера 6.2
    разойдутся уже на 4-м месяце.

    Проверка ловит именно порядок, а не итоговые суммы: `I_3` и `D_3` обязаны
    совпасть с примером 6.1 при любой досрочке, а весь эффект досрочки — уложиться
    в остаток `B_3`. Дополнительно проверяется, что `I_3` не равен «неправильному»
    значению `money((B_2 − E) · i)`, которое дала бы досрочка, списанная до
    начисления процентов.
    """
    amount = Decimal(prepayment)
    rate = monthly_rate(base_loan["annual_rate"])
    base_previous = ref_base_schedule[PREPAYMENT_MONTH - 2]  # строка 2 примера 6.1
    base_row = ref_base_schedule[PREPAYMENT_MONTH - 1]  # строка 3 примера 6.1

    for kind in BUILDERS:
        schedule = build(kind, base_loan, {PREPAYMENT_MONTH: amount})
        row = schedule[PREPAYMENT_MONTH - 1]

        assert_money_equal(
            row.interest, base_row.interest, f"{kind}: проценты месяца {PREPAYMENT_MONTH}"
        )
        assert_money_equal(
            row.interest,
            spec_money(base_previous.balance * rate),
            f"{kind}: проценты месяца {PREPAYMENT_MONTH} от остатка ДО досрочки",
        )
        wrong = spec_money((base_previous.balance - amount) * rate)
        if wrong != row.interest:
            assert row.interest != wrong, (
                f"{kind}: проценты месяца {PREPAYMENT_MONTH} равны {wrong} — это значение "
                f"получается, если досрочку списать ДО начисления процентов "
                f"(раздел 3.6 требует обратного порядка)"
            )
        assert_money_equal(
            row.principal, base_row.principal, f"{kind}: тело месяца {PREPAYMENT_MONTH}"
        )
        assert_money_equal(row.prepayment, amount, f"{kind}: применённая досрочка")
        assert_money_equal(
            row.balance,
            base_row.balance - amount,
            f"{kind}: остаток месяца {PREPAYMENT_MONTH} после досрочки",
        )


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_months_before_prepayment_repeat_the_base_schedule(
    base_loan: dict, ref_base_schedule: list[SpecRow], kind: str
) -> None:
    """Месяцы до досрочки совпадают с базовым графиком примера 6.1 в точности.

    Раздел 3.6 спеки. Досрочка месяца 3 не имеет права задним числом изменить
    месяцы 1 и 2 — ни платёж, ни проценты, ни остаток. Ещё одна проверка порядка:
    реализация, которая уменьшает остаток заранее, разойдётся здесь.
    """
    schedule = build(kind, base_loan, {PREPAYMENT_MONTH: Decimal("100000.00")})
    for expected in ref_base_schedule[: PREPAYMENT_MONTH - 1]:
        actual = schedule[expected.number - 1]
        assert_money_equal(actual.payment, expected.payment, f"{kind}: месяц {expected.number}, платёж")
        assert_money_equal(actual.interest, expected.interest, f"{kind}: месяц {expected.number}, проценты")
        assert_money_equal(actual.principal, expected.principal, f"{kind}: месяц {expected.number}, тело")
        assert_money_equal(actual.prepayment, ZERO, f"{kind}: месяц {expected.number}, досрочка")
        assert_money_equal(actual.balance, expected.balance, f"{kind}: месяц {expected.number}, остаток")


# ------------------------------------------------------- раздел 4.2: находка Н-3


def test_penny_prepayment_does_not_shorten_the_term(
    base_loan: dict, ref_base_schedule: list[SpecRow]
) -> None:
    """Находка Н-3: досрочка `0.01` не сокращает срок и не экономит ничего.

    Раздел 4.2 спеки. Остаток после платежа №3 сдвигается ровно на копейку —
    `761 080.27` против `761 080.28`, — срок остаётся 12 месяцев, экономия `0.00`.
    Тест на строгое сокращение срока (`< n`) упал бы здесь ложно: И-10 нестрогий,
    срок сокращается **на 0 или больше** месяцев.
    """
    args = (base_loan["principal"], base_loan["annual_rate"], base_loan["months"])
    base = schedule_with_term_reduction(*args, {})
    schedule = schedule_with_term_reduction(*args, {PREPAYMENT_MONTH: Decimal("0.01")})

    assert len(schedule) == base_loan["months"], (
        f"копеечная досрочка сократила срок до {len(schedule)} месяцев; "
        f"раздел 4.2 спеки требует прежних {base_loan['months']}"
    )
    assert term_reduction_of(base, schedule) == 0, (
        f"сокращение срока {term_reduction_of(base, schedule)} месяцев при досрочке 0.01; "
        f"раздел 4.2 спеки требует 0"
    )
    assert_money_equal(
        interest_saved(base, schedule), ZERO, "экономия процентов при досрочке 0.01"
    )
    assert_money_equal(
        schedule[PREPAYMENT_MONTH - 1].balance,
        ref_base_schedule[PREPAYMENT_MONTH - 1].balance - Decimal("0.01"),
        "остаток после платежа №3 при досрочке 0.01",
    )
    assert_invariants(schedule, base_loan["principal"], base_loan["months"], "досрочка 0.01")


def test_prepayment_threshold_table_from_spec(
    base_loan: dict, ref_prepayment_thresholds: list
) -> None:
    """Вся таблица порогов раздела 4.2 спеки — находка Н-3, построчно.

    Раздел 4.2. Кредит примера 1, досрочка вносится с платежом №3, вид —
    сокращение срока. Для каждой строки таблицы сверяются число месяцев,
    последний платёж и экономия процентов там, где спека её приводит.

    Здесь и `0.01`, и `1.00` (срок не меняется), и граница `81 237.96 / 81 237.97`
    (переход 12 → 11 месяцев), и `100 000.00` из примера 6.2a. Порог привязан
    именно к этим параметрам кредита — раздел 8.2 спеки предупреждает, что
    универсальной константой он не является, поэтому параметры берутся из `base_loan`.

    Внимание на строку `1.00`: колонки таблицы противоречат друг другу. Последний
    платёж `88 847.67` против `88 848.76` без досрочки — это на `1.09` меньше при
    внесённой `1.00`, то есть процентов сэкономлено ровно `0.09`, а колонка
    «Экономия процентов» показывает `0.00`. Ожидание здесь взято из спеки как есть:
    расхождение чинится правкой документа отдельным коммитом (преамбула спеки),
    а не ослаблением ассерта.
    """
    args = (base_loan["principal"], base_loan["annual_rate"], base_loan["months"])
    base = schedule_with_term_reduction(*args, {})

    assert len(ref_prepayment_thresholds) >= 6, (
        f"в таблице порогов раздела 4.2 разобрано строк: {len(ref_prepayment_thresholds)}, "
        f"ожидалось не меньше 6"
    )

    for row in ref_prepayment_thresholds:
        label = "без досрочки" if row.prepayment is None else f"досрочка {row.prepayment}"
        prepayments = {} if row.prepayment is None else {PREPAYMENT_MONTH: row.prepayment}
        schedule = schedule_with_term_reduction(*args, prepayments)

        assert len(schedule) == row.months, (
            f"{label}: месяцев {len(schedule)}, таблица раздела 4.2 требует {row.months}"
        )
        assert_money_equal(schedule[-1].payment, row.last_payment, f"{label}: последний платёж")
        if row.saved is not None:
            assert_money_equal(
                interest_saved(base, schedule), row.saved, f"{label}: экономия процентов"
            )
        if row.prepayment is not None:
            assert_money_equal(
                schedule[PREPAYMENT_MONTH - 1].prepayment,
                row.prepayment,
                f"{label}: применённая досрочка месяца {PREPAYMENT_MONTH}",
            )
        assert_invariants(schedule, base_loan["principal"], base_loan["months"], label)


def test_threshold_boundary_between_twelve_and_eleven_months(base_loan: dict) -> None:
    """Граница `81 237.96 / 81 237.97`: копейка решает, будет 12 месяцев или 11.

    Раздел 4.2 спеки, находка Н-3. При `81 237.96` срок остаётся 12 месяцев,
    а последний платёж равен `0.01` — это законный результат правила 3.4
    (при сокращении срока условие `k = n` не применяется), а не дефект.
    При `81 237.97` — уже 11 месяцев.

    Тест, требующий «последний платёж сопоставим с регулярным», упал бы здесь
    ложно; тест на строгое сокращение срока — тоже.
    """
    args = (base_loan["principal"], base_loan["annual_rate"], base_loan["months"])
    below = schedule_with_term_reduction(*args, {PREPAYMENT_MONTH: Decimal("81237.96")})
    above = schedule_with_term_reduction(*args, {PREPAYMENT_MONTH: Decimal("81237.97")})

    assert len(below) == 12, (
        f"досрочка 81 237.96 дала {len(below)} месяцев; раздел 4.2 спеки требует 12 — "
        f"это наибольшая досрочка, срок ещё НЕ сокращающая"
    )
    assert_money_equal(below[-1].payment, Decimal("0.01"), "последний платёж при досрочке 81 237.96")
    assert len(above) == 11, (
        f"досрочка 81 237.97 дала {len(above)} месяцев; раздел 4.2 спеки требует 11 — "
        f"это наименьшая досрочка, сокращающая срок"
    )
    assert len(above) < len(below), (
        "переход через порог обязан убирать ровно одну строку графика"
    )
    assert_invariants(below, base_loan["principal"], base_loan["months"], "досрочка 81 237.96")
    assert_invariants(above, base_loan["principal"], base_loan["months"], "досрочка 81 237.97")


# ------------------------------------------------ раздел 5: вырожденные случаи


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_prepayment_larger_than_balance_is_clipped(
    base_loan: dict, ref_base_schedule: list[SpecRow], kind: str
) -> None:
    """Досрочка больше остатка обрезается до остатка и закрывает кредит.

    Раздел 5 спеки: при остатке `761 080.28` внесение `5 000 000.00` даёт
    применённую сумму `761 080.28`, график из 3 строк и остаток `0.00`.
    Излишек не возвращается и не переносится; поле `досрочка` (раздел 4.3)
    обязано показать фактически применённую сумму, а не запрошенную.
    """
    balance_before = ref_base_schedule[PREPAYMENT_MONTH - 1].balance
    schedule = build(kind, base_loan, {PREPAYMENT_MONTH: Decimal("5000000.00")})

    assert len(schedule) == PREPAYMENT_MONTH, (
        f"{kind}: строк {len(schedule)}, спека требует {PREPAYMENT_MONTH} — "
        f"кредит закрывается в месяц внесения досрочки"
    )
    assert_money_equal(
        schedule[-1].prepayment, balance_before, f"{kind}: фактически применённая досрочка"
    )
    assert_money_equal(schedule[-1].balance, ZERO, f"{kind}: остаток после закрытия досрочкой")
    assert_invariants(
        schedule, base_loan["principal"], base_loan["months"], f"{kind}: досрочка больше остатка"
    )


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_prepayment_exactly_equal_to_balance_closes_the_loan(
    base_loan: dict, ref_base_schedule: list[SpecRow], kind: str
) -> None:
    """Досрочка ровно в остаток закрывает кредит в том же месяце, без обрезки.

    Раздел 5 спеки: внесение ровно `761 080.28` даёт 3 строки и остаток `0.00`.
    Граница с предыдущим случаем — та же ветка кода, но без обрезки, поэтому
    оба кейса нужны: дефект «обрезка на единицу» виден только на их паре.
    """
    balance_before = ref_base_schedule[PREPAYMENT_MONTH - 1].balance
    schedule = build(kind, base_loan, {PREPAYMENT_MONTH: balance_before})

    assert len(schedule) == PREPAYMENT_MONTH, (
        f"{kind}: строк {len(schedule)}, спека требует {PREPAYMENT_MONTH}"
    )
    assert_money_equal(schedule[-1].prepayment, balance_before, f"{kind}: применённая досрочка")
    assert_money_equal(schedule[-1].balance, ZERO, f"{kind}: остаток после досрочки в остаток")
    # Месяцы до закрытия по-прежнему повторяют пример 6.1 — досрочка не задним числом.
    assert_money_equal(
        schedule[-1].payment,
        ref_base_schedule[PREPAYMENT_MONTH - 1].payment,
        f"{kind}: платёж месяца закрытия остаётся регулярным",
    )
    assert_invariants(
        schedule, base_loan["principal"], base_loan["months"], f"{kind}: досрочка ровно в остаток"
    )


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_zero_prepayment_changes_nothing(
    base_loan: dict, ref_base_schedule: list[SpecRow], kind: str
) -> None:
    """Досрочка `0.00` оставляет график равным базовому примеру 6.1.

    Разделы 3.6 и 6.1 спеки. Нулевая досрочка — не ошибка (в отличие от нулевой
    суммы кредита, раздел 5), а отсутствие события: график, экономия и срок
    обязаны совпасть с графиком без досрочек до копейки.
    """
    args = (base_loan["principal"], base_loan["annual_rate"], base_loan["months"])
    base = BUILDERS[kind](*args, {})
    schedule = BUILDERS[kind](*args, {PREPAYMENT_MONTH: ZERO})

    assert_schedule_equal(schedule, ref_base_schedule)
    assert_money_equal(interest_saved(base, schedule), ZERO, f"{kind}: экономия при досрочке 0.00")
    assert term_reduction_of(base, schedule) == 0, (
        f"{kind}: досрочка 0.00 сократила срок на {term_reduction_of(base, schedule)} месяцев"
    )


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_empty_prepayments_reproduce_the_base_schedule(
    base_loan: dict, ref_base_schedule: list[SpecRow], kind: str
) -> None:
    """Пустой словарь досрочек даёт ровно график примера 6.1.

    Раздел 6.1 спеки. Это опора всех сравнений: `interest_saved` и `term_reduction`
    измеряются относительно такого графика, и если он сам разошёлся с эталоном,
    любая «экономия» ниже по файлу считается от неверной базы.
    """
    schedule = BUILDERS[kind](
        base_loan["principal"], base_loan["annual_rate"], base_loan["months"], {}
    )
    assert_schedule_equal(schedule, ref_base_schedule)
    assert_invariants(
        schedule, base_loan["principal"], base_loan["months"], f"{kind}: без досрочек"
    )


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
@pytest.mark.parametrize("amount", ["-0.01", "-1.00", "-100000.00"], ids=lambda t: f"E={t}")
def test_negative_prepayment_is_rejected(base_loan: dict, kind: str, amount: str) -> None:
    """Отрицательная досрочка отвергается `ValueError`, расчёт не выполняется.

    Раздел 5 спеки трактует отрицательные денежные величины как ошибку валидации,
    инвариант И-7 требует `E_k ≥ 0`. Отрицательная досрочка — это выдача денег
    обратно, она увеличила бы остаток и сломала И-5.
    """
    with pytest.raises(ValueError):
        BUILDERS[kind](
            base_loan["principal"],
            base_loan["annual_rate"],
            base_loan["months"],
            {PREPAYMENT_MONTH: Decimal(amount)},
        )


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
@pytest.mark.parametrize("month", [0, -1, 13, 99], ids=lambda m: f"k={m}")
def test_prepayment_on_month_outside_the_term_is_rejected(
    base_loan: dict, kind: str, month: int
) -> None:
    """Досрочка на несуществующий месяц отвергается `ValueError`.

    Раздел 2 спеки задаёт `k` как номер платёжного месяца в диапазоне `1 … n`;
    ключ вне этого диапазона — недопустимые входные данные. Молча проигнорировать
    такую досрочку нельзя: заёмщик внёс деньги, а график их не показал бы, и дефект
    вызывающего кода остался бы незамеченным (та же логика, что у нулевой суммы
    кредита в разделе 5).
    """
    with pytest.raises(ValueError):
        BUILDERS[kind](
            base_loan["principal"],
            base_loan["annual_rate"],
            base_loan["months"],
            {month: Decimal("1000.00")},
        )


def test_prepayment_with_zero_rate_and_term_reduction() -> None:
    """Досрочка при ставке 0 %, вид «сокращение срока».

    Раздел 5 спеки, строка «Ставка 0 %»: кредит `120 000.00 / 0 % / 12 мес`,
    `A = money(S / n) = 10 000.00`, все `I_k = 0.00`. Досрочка `30 000.00`
    с платежом №3 убирает ровно три месяца: после платежа №3 остаток
    `90 000.00 − 30 000.00 = 60 000.00`, то есть ещё шесть платежей по `10 000.00`.

    Экономии процентов здесь нет и быть не может — при нулевой ставке досрочка
    сокращает срок, не экономя ни копейки. Проверка И-4 в форме «все проценты
    равны нулю» тоже относится сюда.
    """
    loan = {"principal": Decimal("120000.00"), "annual_rate": Decimal("0"), "months": 12}
    args = (loan["principal"], loan["annual_rate"], loan["months"])
    base = schedule_with_term_reduction(*args, {})
    schedule = schedule_with_term_reduction(*args, {PREPAYMENT_MONTH: Decimal("30000.00")})

    assert len(schedule) == 9, (
        f"при ставке 0 % и досрочке 30 000.00 месяцев {len(schedule)}, ожидается 9: "
        f"остаток 60 000.00 после месяца 3 гасится шестью платежами по 10 000.00"
    )
    assert term_reduction_of(base, schedule) == 3, (
        f"сокращение срока {term_reduction_of(base, schedule)} месяцев, ожидается 3"
    )
    assert_money_equal(interest_saved(base, schedule), ZERO, "экономия процентов при ставке 0 %")
    for row in schedule:
        assert_money_equal(row.interest, ZERO, f"месяц {row.number}, проценты при ставке 0 %")
        assert_money_equal(row.payment, Decimal("10000.00"), f"месяц {row.number}, платёж")
    assert_money_equal(
        schedule[PREPAYMENT_MONTH - 1].prepayment, Decimal("30000.00"), "применённая досрочка"
    )
    assert_money_equal(total_paid(schedule), loan["principal"], "всего выплачено при ставке 0 %")
    assert_invariants(schedule, loan["principal"], loan["months"], "ставка 0 %, сокращение срока")


def test_prepayment_with_zero_rate_and_payment_reduction() -> None:
    """Досрочка при ставке 0 %, вид «уменьшение платежа».

    Раздел 5 (ставка 0 %) и раздел 3.6 (`A' = money(annuity(B_k, i, n − k))`).
    Кредит `120 000.00 / 0 % / 12 мес`, досрочка `30 000.00` с платежом №3:
    остаток `60 000.00` делится на 9 оставшихся месяцев, `A' = money(60 000 / 9) =
    6 666.67`. Срок остаётся 12 месяцев, последний платёж балансирующий —
    `60 000.00 − 8 · 6 666.67 = 6 666.64`, то есть снова **меньше** регулярного,
    в отличие от находки Н-1.
    """
    loan = {"principal": Decimal("120000.00"), "annual_rate": Decimal("0"), "months": 12}
    schedule = schedule_with_payment_reduction(
        loan["principal"], loan["annual_rate"], loan["months"], {PREPAYMENT_MONTH: Decimal("30000.00")}
    )

    assert len(schedule) == loan["months"], (
        f"при уменьшении платежа срок фиксирован: строк {len(schedule)}, ожидается {loan['months']}"
    )
    for row in schedule[:PREPAYMENT_MONTH]:
        assert_money_equal(row.payment, Decimal("10000.00"), f"месяц {row.number}, платёж до досрочки")
    for row in schedule[PREPAYMENT_MONTH:-1]:
        assert_money_equal(
            row.payment, Decimal("6666.67"), f"месяц {row.number}, пересчитанный платёж"
        )
    assert_money_equal(schedule[-1].payment, Decimal("6666.64"), "последний платёж, ставка 0 %")
    for row in schedule:
        assert_money_equal(row.interest, ZERO, f"месяц {row.number}, проценты при ставке 0 %")
    assert_invariants(schedule, loan["principal"], loan["months"], "ставка 0 %, уменьшение платежа")


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_prepayment_on_single_month_loan(kind: str) -> None:
    """Досрочка на кредите сроком 1 месяц применяется в нулевом размере.

    Раздел 5 спеки, строка «Срок 1 месяц»: `100 000.00 / 12 % / 1 мес` даёт
    единственный балансирующий платёж `A_1 = 101 000.00`, `I_1 = 1 000.00`,
    `D_1 = 100 000.00`, `B_1 = 0`. По разделу 3.6 досрочка списывается ПОСЛЕ
    разнесения платежа и обрезается остатком: `E_1 = min(E_1, 0.00) = 0.00`.

    Иначе говоря, гасить досрочно уже нечего — платёж закрыл долг целиком.
    Спека не разбирает этот случай отдельно, но он однозначно следует
    из связки правил 3.4 и 3.6.
    """
    loan = {"principal": Decimal("100000.00"), "annual_rate": Decimal("0.12"), "months": 1}
    schedule = build(kind, loan, {1: Decimal("50000.00")})

    assert len(schedule) == 1, f"{kind}: строк {len(schedule)}, спека требует одну"
    row = schedule[0]
    assert_money_equal(row.payment, Decimal("101000.00"), f"{kind}: единственный платёж")
    assert_money_equal(row.interest, Decimal("1000.00"), f"{kind}: проценты")
    assert_money_equal(row.principal, Decimal("100000.00"), f"{kind}: тело")
    assert_money_equal(row.prepayment, ZERO, f"{kind}: применённая досрочка")
    assert_money_equal(row.balance, ZERO, f"{kind}: остаток")
    assert_invariants(schedule, loan["principal"], loan["months"], f"{kind}: срок 1 месяц")


# ------------------------------------------------------ раздел 4: инварианты


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
@pytest.mark.parametrize(
    ("principal", "annual_rate", "months", "prepayments"),
    [
        ("1000000.00", "0.12", 12, {3: "100000.00"}),
        ("1000000.00", "0.12", 12, {1: "0.01"}),
        ("1000000.00", "0.12", 12, {3: "81237.96"}),
        ("1000000.00", "0.12", 12, {3: "81237.97"}),
        ("1000000.00", "0.12", 12, {2: "50000.00", 5: "50000.00", 7: "1.00"}),
        ("1000000.00", "0.12", 12, {12: "1000.00"}),
        ("100.00", "0.001", 12, {6: "10.00"}),
        ("120000.00", "0", 12, {4: "20000.00"}),
        ("100000.00", "0.12", 1, {1: "1000.00"}),
        ("50000.00", "0.095", 36, {12: "10000.00", 24: "5000.00"}),
    ],
    ids=[
        "ref_100k_month3",
        "penny_month1",
        "threshold_below",
        "threshold_above",
        "three_prepayments",
        "last_month",
        "tiny_loan_from_finding_2",
        "zero_rate",
        "single_month_term",
        "three_years_two_prepayments",
    ],
)
def test_invariants_hold_for_schedules_with_prepayments(
    kind: str, principal: str, annual_rate: str, months: int, prepayments: dict
) -> None:
    """Инварианты раздела 4 держатся на графиках с досрочками.

    Раздел 4 спеки: «обязательны всегда, в любом сценарии, включая вырожденные».
    Проверяются И-1 (`Σ(D_k + E_k) = S` — именно здесь досрочка обязана входить
    в сумму тел), И-2, И-4 (нестрого — находка Н-2), И-5, И-6, И-7, И-8,
    И-9 (`ΣA + ΣE = S + ΣI`) и И-10 (число строк не больше `n`).

    Набор кейсов включает несколько досрочек в одном графике, досрочку в первый
    и в последний месяц, ставку 0 %, ставку `0.1 %` из находки Н-2 и срок 1 месяц.
    """
    schedule = BUILDERS[kind](
        Decimal(principal),
        Decimal(annual_rate),
        months,
        {month: Decimal(amount) for month, amount in prepayments.items()},
    )
    assert_invariants(
        schedule,
        Decimal(principal),
        months,
        f"{kind}: {principal} / {annual_rate} / {months} мес, досрочки {prepayments}",
    )


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_prepayment_never_increases_total_interest(base_loan: dict, kind: str) -> None:
    """Досрочка не увеличивает переплату: экономия неотрицательна.

    Раздел 4.2 спеки: копеечная досрочка экономит `0.00` — то есть ноль допустим,
    а отрицательная экономия означала бы, что досрочное погашение сделало кредит
    дороже. Проверяется на возрастающем ряде досрочек: экономия обязана расти
    не убывая вместе с суммой досрочки.
    """
    args = (base_loan["principal"], base_loan["annual_rate"], base_loan["months"])
    base = BUILDERS[kind](*args, {})
    previous_saved = ZERO
    for amount in ("0.01", "1.00", "1000.00", "81237.97", "100000.00", "500000.00"):
        schedule = BUILDERS[kind](*args, {PREPAYMENT_MONTH: Decimal(amount)})
        saved = interest_saved(base, schedule)
        assert saved >= ZERO, (
            f"{kind}: досрочка {amount} дала экономию {saved} — досрочное погашение "
            f"не имеет права увеличивать переплату"
        )
        assert saved >= previous_saved, (
            f"{kind}: досрочка {amount} сэкономила {saved}, а меньшая досрочка — "
            f"{previous_saved}; экономия обязана не убывать с ростом досрочки"
        )
        assert total_interest(schedule) <= total_interest(base), (
            f"{kind}: проценты с досрочкой {amount} превысили проценты без досрочки"
        )
        previous_saved = saved


# --------------------------------------------------- раздел 1: запрет float и валидация


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_float_prepayment_is_rejected(base_loan: dict, kind: str) -> None:
    """Досрочка типа `float` отвергается `TypeError`, а не округляется молча.

    Раздел 1 спеки: деньги — `Decimal`, создаваемый только из строк; `float`
    запрещён на любом пути. `100000.1` в двоичной плавающей точке — это
    `100000.09999999999...`, и потеря копейки всплывёт только на последнем платеже.
    """
    with pytest.raises(TypeError):
        BUILDERS[kind](
            base_loan["principal"],
            base_loan["annual_rate"],
            base_loan["months"],
            {PREPAYMENT_MONTH: 100000.0},
        )


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
def test_float_loan_parameters_are_rejected(kind: str) -> None:
    """Тело кредита и ставка типа `float` отвергаются `TypeError`.

    Раздел 1 спеки. Проверяется вход именно через функции досрочки: валидация
    обязана стоять на каждом публичном пути, а не только в базовом построителе.
    """
    prepayments = {PREPAYMENT_MONTH: Decimal("1000.00")}
    with pytest.raises(TypeError):
        BUILDERS[kind](1000000.0, Decimal("0.12"), 12, prepayments)
    with pytest.raises(TypeError):
        BUILDERS[kind](Decimal("1000000.00"), 0.12, 12, prepayments)


@pytest.mark.parametrize("kind", list(BUILDERS), ids=BUILDER_IDS)
@pytest.mark.parametrize(
    ("principal", "annual_rate", "months", "reason"),
    [
        ("0.00", "0.12", 12, "нулевая сумма — раздел 5, S_req > 0"),
        ("-1000.00", "0.12", 12, "отрицательная сумма — раздел 5"),
        ("1000000.00", "-0.01", 12, "отрицательная ставка — раздел 5, r ≥ 0"),
        ("1000000.00", "0.12", 0, "нулевой срок — раздел 2, n ≥ 1"),
        ("1000000.00", "0.12", -12, "отрицательный срок — раздел 2, n ≥ 1"),
    ],
    ids=["zero_principal", "negative_principal", "negative_rate", "zero_months", "negative_months"],
)
def test_invalid_loan_parameters_are_rejected(
    kind: str, principal: str, annual_rate: str, months: int, reason: str
) -> None:
    """Недопустимые параметры кредита отвергаются `ValueError` и через API досрочки.

    Раздел 5 спеки: нулевая и отрицательная сумма, отрицательная ставка —
    ошибки валидации, расчёт не выполняется. Пустой график допустимым результатом
    не является: он замаскировал бы дефект вызывающего кода.
    """
    try:
        schedule = BUILDERS[kind](
            Decimal(principal), Decimal(annual_rate), months, {PREPAYMENT_MONTH: ZERO}
        )
    except ValueError:
        return
    pytest.fail(
        f"{kind}: параметры приняты без ошибки (построено строк: {len(schedule)}), "
        f"хотя {reason}"
    )


# --------------------------------------------------------------------------- BUG-01

#: Длинный кредит с **отрицательным** запасом последнего платежа.
#: Регулярный платёж `36 005.04`, последний `36 005.88` — последний больше
#: регулярного, в отличие от 12-месячного эталона спеки, где он меньше.
LONG_LOAN = (Decimal("3000000.00"), Decimal("0.12"), 180)


@pytest.mark.xfail(
    raises=ScheduleDidNotTerminate,
    strict=True,
    reason=(
        "BUG-01: при сокращении срока условие (б) правила 3.4 не применяется, "
        "и кредиту с отрицательным запасом последнего платежа требуется строка n+1, "
        "которую запрещает И-10. См. findings.md, BUG-01 и BUG-02"
    ),
)
def test_penny_prepayment_on_long_loan_closes_the_schedule() -> None:
    """Копеечная досрочка на длинном кредите обязана дать закрытый график.

    Раздел 4.2 спеки прямо разрешает при сокращении срока последнюю строку сколь
    угодно малого размера, вплоть до `0.01`, и называет это законным результатом.
    Инвариант И-2 («остаток после последнего платежа строго ноль») объявлен
    безусловным и исключений не имеет.

    Фактически расчёт падает с `ScheduleDidNotTerminate`: график требует строки
    `n + 1`, а И-10 разрешает не более `n`. Дефект избирателен — досрочки `1.00`
    и `100.00` на том же кредите считаются нормально, падает именно `0.01`.

    Ожидания в этом тесте намеренно **не** ослаблены под фактическое поведение:
    он описывает то, что требует спека, и помечен `xfail(strict=True)`, чтобы
    прогон оставался зелёным, а починка BUG-01 немедленно проявилась как
    неожиданный проход.
    """
    principal, annual_rate, months = LONG_LOAN
    schedule = schedule_with_term_reduction(
        principal, annual_rate, months, {PREPAYMENT_MONTH: Decimal("0.01")}
    )

    assert_money_equal(schedule[-1].balance, ZERO, "остаток после последнего платежа")
    assert_money_equal(
        sum_money(row.principal + row.prepayment for row in schedule),
        principal,
        "сумма тел с досрочками",
    )
