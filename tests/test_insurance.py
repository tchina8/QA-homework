"""Регресс страховки: обе схемы — из своих средств и в кредит.

Источник ожиданий — `docs/spec.md`, разделы 3.5 (формула премии), 5 (вырожденные
случаи), 6.3 (эталонный пример 3, обе схемы), 4 (инварианты), 8.1–8.2 (границы
области и известные слабые места). Реализация в `calc/` при написании этих тестов
не открывалась намеренно: тест, списанный с кода, проверяет лишь то, что код равен
сам себе.

Главная проверяемая идея раздела 3.5 — премия считается от **запрошенной** суммы
`S_req`, а не от тела кредита. Спека называет это решением с ценой (раздел 8.2):
если реализация возьмёт базой тело, все числа примера 6.3b поедут, а сам факт
подмены базы на «красивых» входных данных незаметен. Поэтому подмена базы вынесена
в отдельный тест с явным контрпримером.

Аннуитет без страховки и досрочное погашение проверяются в соседних модулях —
здесь они затрагиваются только там, где этого требует раздел 6.3.

Не покрыто намеренно (раздел 8.1 спеки): ежегодная страховка, ежемесячная страховка,
скидка к ставке за страховку. Их в модели нет, и тестов на них здесь нет —
область зафиксирована тестом `test_annual_monthly_and_discount_schemes_are_out_of_scope`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import ZERO, assert_money_equal, assert_schedule_equal, sum_money

from calc.insurance import (
    insurance_premium,
    loan_with_financed_insurance,
    loan_with_own_funds_insurance,
)

# --------------------------------------------------------------------------- константы

#: Вход эталонного примера 6.3 спеки: `S_req = 1 000 000.00`, `ρ = 5 %`, `r = 12 %`, `n = 12`.
REF_REQUESTED = Decimal("1000000.00")
REF_RATE = Decimal("0.12")
REF_MONTHS = 12
REF_INSURANCE_RATE = Decimal("0.05")

#: Премия эталонного примера 6.3: `money(1 000 000.00 · 0.05)`.
REF_PREMIUM = Decimal("50000.00")

#: Итоги примера 6.3a — страховка из своих средств (тело равно `S_req`).
REF_OWN_PAID_TO_BANK = Decimal("1066185.45")
REF_OWN_INTEREST = Decimal("66185.45")
REF_OWN_OUT_OF_POCKET = Decimal("1116185.45")

#: Итоги примера 6.3b — страховка в кредит (тело равно `S_req + p`).
REF_FINANCED_BODY = Decimal("1050000.00")
REF_FINANCED_PAYMENT = Decimal("93291.23")
REF_FINANCED_LAST_PAYMENT = Decimal("93291.22")
REF_FINANCED_PAID_TO_BANK = Decimal("1119494.75")
REF_FINANCED_INTEREST = Decimal("69494.75")

#: Ключевая сверка раздела 6.3: насколько страховка в кредит дороже страховки
#: из своих средств при одинаковой премии. Это проценты, начисленные на премию.
REF_INSURANCE_OVERPAY = Decimal("3309.30")

#: Премия, которую дала бы **ошибочная** база — тело кредита вместо `S_req`.
#: Из `p = (S_req + p) · ρ` при `ρ = 5 %` следует `p = S_req · ρ / (1 − ρ)`.
WRONG_BASE_PREMIUM = Decimal("52631.58")
WRONG_BASE_BODY = Decimal("1052631.58")


def own_funds(requested, annual_rate, months, insurance_rate):
    """Схема «из своих средств» с денежными аргументами-строками.

    Вход: суммы и ставки строками, срок — целым.
    Выход: `InsuranceResult`.

    Обёртка нужна только чтобы в теле теста не тонуть в `Decimal(...)`:
    раздел 1 спеки запрещает `float`, поэтому строка — единственный законный путь.
    """
    return loan_with_own_funds_insurance(
        Decimal(requested), Decimal(annual_rate), months, Decimal(insurance_rate)
    )


def financed(requested, annual_rate, months, insurance_rate):
    """Схема «в кредит» с денежными аргументами-строками.

    Вход: суммы и ставки строками, срок — целым.
    Выход: `InsuranceResult`.
    """
    return loan_with_financed_insurance(
        Decimal(requested), Decimal(annual_rate), months, Decimal(insurance_rate)
    )


#: Обе схемы под одним ключом — для тестов, которые обязаны выполняться на каждой.
SCHEMES = {"own_funds": own_funds, "financed": financed}


def assert_two_decimals(value: Decimal, label: str) -> None:
    """Проверить масштаб денежной величины — ровно 2 знака (инвариант И-8).

    Вход: величина и подпись. Выход: `None`; при нарушении — `AssertionError`.
    """
    assert isinstance(value, Decimal), f"{label}: ожидается Decimal, получен {type(value).__name__}"
    assert -value.as_tuple().exponent == 2, (
        f"{label}: {value} записано не с 2 знаками после запятой (инвариант И-8)"
    )


def assert_schedule_invariants(schedule, loan_body: Decimal, months: int, label: str) -> None:
    """Проверить инварианты раздела 4 спеки на графике со страховкой.

    Вход: график, тело кредита `S` (для схемы «в кредит» это `S_req + p`),
    заявленный срок `n` и подпись сценария.
    Выход: `None`; при нарушении — `AssertionError` с номером инварианта.

    Проверяются И-1 … И-10. Ключевой для страховки — И-1: сумма тел сходится
    именно с **телом кредита**, а не с запрошенной суммой; для схемы «в кредит»
    это разные числа, и подмена одного другим — ровно тот дефект, который
    раздел 8.2 называет вероятным.

    В И-3 `money(B_{last−1} + I_last)` записано как обычное сложение: оба
    слагаемых по И-8 уже приведены к копейкам, их сумма точна, и лишний вызов
    округления только спрятал бы расхождение.
    """
    assert schedule, f"{label}: график пуст, а пустой график по разделу 5 спеки не результат"

    numbers = [row.number for row in schedule]
    assert numbers == list(range(1, len(schedule) + 1)), (
        f"{label}: номера месяцев идут не подряд с 1 — {numbers}"
    )

    # И-8: ровно 2 знака у каждой денежной величины графика.
    for row in schedule:
        for field in ("payment", "interest", "principal", "prepayment", "balance"):
            assert_two_decimals(getattr(row, field), f"{label}: месяц {row.number}, {field}")

    # И-1: сумма тел (вместе с досрочками) равна телу кредита.
    body_sum = sum_money(row.principal + row.prepayment for row in schedule)
    assert_money_equal(body_sum, loan_body, f"{label}: И-1, сумма тел")

    # И-2: остаток после последнего платежа строго ноль, без допуска.
    assert schedule[-1].balance == ZERO, (
        f"{label}: И-2, остаток после последнего платежа {schedule[-1].balance}, "
        f"спека требует ровно 0.00"
    )

    # И-3: последний платёж балансирующий и он единственный, отличный от регулярного.
    regular = schedule[0].payment
    deviating = [row.number for row in schedule[1:] if row.payment != regular]
    assert deviating in ([], [len(schedule)]), (
        f"{label}: И-3, от регулярного платежа {regular} отличаются месяцы {deviating}; "
        f"спека разрешает отличаться только последнему"
    )
    previous_balance = schedule[-2].balance if len(schedule) > 1 else loan_body
    assert_money_equal(
        schedule[-1].payment,
        previous_balance + schedule[-1].interest,
        f"{label}: И-3, балансирующий последний платёж",
    )

    # И-4: проценты не возрастают. Неравенство нестрогое — раздел 4.1, находка Н-2.
    for previous, current in zip(schedule, schedule[1:]):
        assert previous.interest >= current.interest, (
            f"{label}: И-4, проценты выросли с {previous.interest} (месяц {previous.number}) "
            f"до {current.interest} (месяц {current.number})"
        )

    # И-5: остаток убывает строго, начиная с тела кредита.
    balance_before = loan_body
    for row in schedule:
        assert row.balance < balance_before, (
            f"{label}: И-5, месяц {row.number}: остаток {row.balance} не меньше "
            f"предыдущего {balance_before}"
        )
        balance_before = row.balance

    for row in schedule:
        # И-6: платёж покрывает проценты, тело строго положительно.
        assert row.payment > row.interest, (
            f"{label}: И-6, месяц {row.number}: платёж {row.payment} не покрывает "
            f"проценты {row.interest}"
        )
        assert row.principal > ZERO, (
            f"{label}: И-6, месяц {row.number}: тело платежа {row.principal} не положительно"
        )
        # И-7: все величины неотрицательны.
        for field in ("payment", "interest", "principal", "prepayment", "balance"):
            assert getattr(row, field) >= ZERO, (
                f"{label}: И-7, месяц {row.number}: {field} отрицательно "
                f"({getattr(row, field)})"
            )

    # И-9: сумма выплат сходится.
    paid = sum_money(row.payment for row in schedule) + sum_money(row.prepayment for row in schedule)
    interest = sum_money(row.interest for row in schedule)
    assert_money_equal(paid, loan_body + interest, f"{label}: И-9, сумма выплат")

    # И-10: график конечен и срок не растёт.
    assert len(schedule) <= months, (
        f"{label}: И-10, в графике {len(schedule)} строк при сроке {months} месяцев"
    )


def assert_result_consistent(result, label: str) -> None:
    """Проверить, что агрегаты результата сходятся с его же графиком.

    Вход: `InsuranceResult` и подпись. Выход: `None`.

    Раздел 3.5 спеки: при схеме «из своих средств» премия платится вне графика
    и полная нагрузка равна сумме платежей плюс `p`; при схеме «в кредит» премия
    уже внутри графика, поэтому нагрузка равна выплатам банку.
    """
    assert result.scheme in ("own_funds", "financed"), (
        f"{label}: неизвестное имя схемы {result.scheme!r}"
    )
    assert_money_equal(
        result.paid_to_bank,
        sum_money(row.payment for row in result.schedule)
        + sum_money(row.prepayment for row in result.schedule),
        f"{label}: выплачено банку против суммы строк графика",
    )
    assert_money_equal(
        result.interest,
        sum_money(row.interest for row in result.schedule),
        f"{label}: проценты против суммы процентов графика",
    )
    expected_pocket = (
        result.paid_to_bank + result.premium
        if result.scheme == "own_funds"
        else result.paid_to_bank
    )
    assert_money_equal(
        result.out_of_pocket,
        expected_pocket,
        f"{label}: итого из кармана (раздел 3.5, денежный поток заёмщика)",
    )


# --------------------------------------------------------- 3.5. формула премии


def test_premium_is_computed_from_requested_amount() -> None:
    """Премия равна `money(S_req · ρ)` — раздел 3.5 спеки.

    Эталон раздела 6.3: `money(1 000 000.00 · 0.05) = 50 000.00`. Значение
    проверяется и у самой функции, и у обеих схем: премия обязана быть одной
    и той же величиной независимо от того, кто её платит.
    """
    assert_money_equal(
        insurance_premium(REF_REQUESTED, REF_INSURANCE_RATE), REF_PREMIUM, "премия 6.3"
    )
    own = own_funds("1000000.00", "0.12", 12, "0.05")
    fin = financed("1000000.00", "0.12", 12, "0.05")
    assert_money_equal(own.premium, REF_PREMIUM, "премия схемы «из своих средств»")
    assert_money_equal(fin.premium, REF_PREMIUM, "премия схемы «в кредит»")


def test_premium_base_is_requested_amount_not_loan_body() -> None:
    """База премии — `S_req`, а не тело кредита (разделы 3.5 и 8.2).

    Раздел 8.2 спеки прямо называет выбор базы решением с ценой: часть банков
    считает премию от полного тела, и тогда все числа примера 6.3b другие.
    Поэтому проверяется не только правильное значение `50 000.00`, но и явное
    отсутствие значения `52 631.58`, которое дала бы круговая формула
    `p = (S_req + p) · ρ`. Без второй половины теста подмена базы прошла бы
    незамеченной на любом «круглом» входе.
    """
    fin = financed("1000000.00", "0.12", 12, "0.05")
    assert_money_equal(fin.premium, REF_PREMIUM, "премия схемы «в кредит»")
    assert fin.premium != WRONG_BASE_PREMIUM, (
        f"премия {fin.premium} посчитана от тела кредита, а раздел 3.5 спеки требует "
        f"базу S_req: ожидалось {REF_PREMIUM}, круговая формула дала бы {WRONG_BASE_PREMIUM}"
    )
    assert_money_equal(fin.loan_body, REF_FINANCED_BODY, "тело кредита схемы «в кредит»")
    assert fin.loan_body != WRONG_BASE_BODY, (
        f"тело кредита {fin.loan_body} соответствует премии от тела, "
        f"а спека требует {REF_FINANCED_BODY}"
    )


@pytest.mark.parametrize(
    ("annual_rate", "months"),
    [("0.12", 12), ("0.00", 12), ("0.095", 60), ("0.12", 1)],
    ids=["12%/12", "0%/12", "9.5%/60", "12%/1"],
)
def test_premium_does_not_depend_on_rate_and_term(annual_rate: str, months: int) -> None:
    """Премия зависит только от `S_req` и `ρ` — раздел 3.5 спеки.

    В формуле `p = money(S_req · ρ)` нет ни ставки, ни срока. Если премия
    поедет при смене ставки или срока, значит в базу просочилось тело кредита
    или начисленные проценты.
    """
    for name, call in SCHEMES.items():
        result = call("1000000.00", annual_rate, months, "0.05")
        assert_money_equal(result.premium, REF_PREMIUM, f"премия схемы {name} при {annual_rate}/{months}")


@pytest.mark.parametrize(
    ("requested", "insurance_rate", "expected"),
    [
        ("1000000.00", "0.05", "50000.00"),
        ("0.10", "0.05", "0.01"),
        ("0.50", "0.05", "0.03"),
        ("0.30", "0.05", "0.02"),
        ("100.10", "0.05", "5.01"),
        ("0.01", "0.05", "0.00"),
        ("0.20", "0.05", "0.01"),
    ],
    ids=["эталон", "0.005", "0.025", "0.015", "5.005", "ниже-полукопейки", "ровно-копейка"],
)
def test_premium_rounds_half_up(requested: str, insurance_rate: str, expected: str) -> None:
    """Премия округляется по `ROUND_HALF_UP` — раздел 1 спеки.

    Кейсы подобраны по границе половины копейки, где `ROUND_HALF_UP` расходится
    с режимом `decimal` по умолчанию: `0.005 → 0.01` (а не `0.00`),
    `0.025 → 0.03` (а не `0.02`), `5.005 → 5.01` (а не `5.00`). На обычных
    суммах оба режима совпадают, поэтому дефект ловится только здесь.
    """
    assert_money_equal(
        insurance_premium(Decimal(requested), Decimal(insurance_rate)),
        Decimal(expected),
        f"премия от {requested} по тарифу {insurance_rate}",
    )


# ------------------------------------------------- 6.3a. страховка из своих средств


def test_own_funds_loan_body_equals_requested_amount() -> None:
    """При страховке из своих средств тело кредита равно `S_req` — раздел 3.5.

    Премия платится вне графика, поэтому в кредит она не попадает и процентов
    на себя не собирает.
    """
    own = own_funds("1000000.00", "0.12", 12, "0.05")
    assert own.scheme == "own_funds", f"имя схемы: получено {own.scheme!r}"
    assert_money_equal(own.loan_body, REF_REQUESTED, "тело кредита схемы «из своих средств»")


def test_own_funds_schedule_matches_reference_example(ref_base_schedule) -> None:
    """График схемы «из своих средств» совпадает с примером 6.1 — раздел 6.3a.

    Спека говорит об этом прямо: тело равно запрошенной сумме, значит график
    обязан быть тем же самым базовым аннуитетом, включая балансирующий последний
    платёж `88 848.76`. Любое расхождение означает, что премия просочилась
    в тело кредита.
    """
    own = own_funds("1000000.00", "0.12", 12, "0.05")
    assert_schedule_equal(own.schedule, ref_base_schedule)


def test_own_funds_totals_match_spec() -> None:
    """Итоги схемы «из своих средств» — раздел 6.3a спеки.

    Банку `1 066 185.45`, премия `50 000.00` отдельно, итого из кармана
    `1 116 185.45`. Премия учтена именно как отдельная строка расхода:
    в выплатах банку её быть не должно.
    """
    own = own_funds("1000000.00", "0.12", 12, "0.05")
    assert_money_equal(own.paid_to_bank, REF_OWN_PAID_TO_BANK, "выплачено банку, 6.3a")
    assert_money_equal(own.premium, REF_PREMIUM, "премия отдельно, 6.3a")
    assert_money_equal(own.interest, REF_OWN_INTEREST, "проценты, 6.3a")
    assert_money_equal(own.out_of_pocket, REF_OWN_OUT_OF_POCKET, "итого из кармана, 6.3a")
    assert_result_consistent(own, "6.3a")


# ----------------------------------------------------- 6.3b. страховка в кредит


def test_financed_loan_body_is_requested_plus_premium() -> None:
    """При страховке в кредит тело равно `S_req + p` — разделы 3.5 и 6.3b.

    `1 000 000.00 + 50 000.00 = 1 050 000.00`. Именно на эту сумму, а не на
    запрошенную, начисляются проценты.
    """
    fin = financed("1000000.00", "0.12", 12, "0.05")
    assert fin.scheme == "financed", f"имя схемы: получено {fin.scheme!r}"
    assert_money_equal(fin.loan_body, REF_FINANCED_BODY, "тело кредита схемы «в кредит»")
    assert_money_equal(
        fin.loan_body, REF_REQUESTED + fin.premium, "тело кредита против S_req + p"
    )


def test_financed_schedule_matches_spec_row_by_row(ref_financed_insurance_schedule) -> None:
    """График схемы «в кредит» сверяется с таблицей 6.3b построчно.

    Все 12 строк: платёж, проценты, тело, досрочка и остаток каждого месяца.
    Ожидания разбираются из самой спеки, а не переписаны в код теста.
    """
    fin = financed("1000000.00", "0.12", 12, "0.05")
    assert len(ref_financed_insurance_schedule) == 12, (
        "в таблице 6.3b спеки должно быть 12 строк, разобрано "
        f"{len(ref_financed_insurance_schedule)} — сломан разбор спеки, а не код"
    )
    assert_schedule_equal(fin.schedule, ref_financed_insurance_schedule)


def test_financed_regular_and_balancing_payments() -> None:
    """Регулярный платёж `93 291.23`, последний — балансирующий `93 291.22` (6.3b, Н-1).

    Первые 11 месяцев платёж постоянен, последний отличается: по И-3 он равен
    `money(B_11 + I_12)` и здесь оказывается **меньше** регулярного. Знак
    балансировки не фиксирован — раздел 6.2 спеки предупреждает, что в примере 2b
    последний платёж, наоборот, на копейку больше. Поэтому проверяется равенство
    формуле И-3, а не сторона отклонения; конкретное `93 291.22` берётся
    из таблицы 6.3b как эталон.
    """
    fin = financed("1000000.00", "0.12", 12, "0.05")
    schedule = fin.schedule
    assert len(schedule) == 12, (
        f"график 6.3b обязан содержать 12 строк, получено {len(schedule)}"
    )
    for row in schedule[:-1]:
        assert_money_equal(
            row.payment, REF_FINANCED_PAYMENT, f"месяц {row.number}, регулярный платёж 6.3b"
        )
    assert_money_equal(
        schedule[-1].payment, REF_FINANCED_LAST_PAYMENT, "последний платёж 6.3b"
    )
    assert_money_equal(
        schedule[-1].payment,
        schedule[-2].balance + schedule[-1].interest,
        "И-3, последний платёж 6.3b как money(B_11 + I_12)",
    )


def test_financed_totals_match_spec() -> None:
    """Итоги схемы «в кредит» — раздел 6.3b спеки.

    Банку `1 119 494.75`, проценты `69 494.75`, сумма тел `1 050 000.00`,
    итого из кармана те же `1 119 494.75`: премия уже внутри графика
    и второй раз в карман не попадает.
    """
    fin = financed("1000000.00", "0.12", 12, "0.05")
    assert_money_equal(fin.paid_to_bank, REF_FINANCED_PAID_TO_BANK, "выплачено банку, 6.3b")
    assert_money_equal(fin.interest, REF_FINANCED_INTEREST, "проценты, 6.3b")
    assert_money_equal(fin.premium, REF_PREMIUM, "премия, 6.3b")
    assert_money_equal(
        fin.out_of_pocket, REF_FINANCED_PAID_TO_BANK, "итого из кармана, 6.3b"
    )
    assert_money_equal(
        sum_money(row.principal for row in fin.schedule),
        REF_FINANCED_BODY,
        "сумма тел, 6.3b",
    )
    assert_result_consistent(fin, "6.3b")


# --------------------------------------------- 6.3. ключевая сверка двух схем


def test_financed_insurance_costs_exactly_3309_30_more() -> None:
    """Страховка в кредит дороже страховки из своих средств ровно на `3 309.30`.

    Ключевая сверка раздела 6.3: спека требует проверять именно разницу,
    а не только графики по отдельности. Премия у схем одинаковая (`50 000.00`),
    поэтому вся разница — это проценты, начисленные на саму премию; она обязана
    сойтись и по нагрузке на карман, и по сумме процентов.
    """
    own = own_funds("1000000.00", "0.12", 12, "0.05")
    fin = financed("1000000.00", "0.12", 12, "0.05")

    assert_money_equal(own.premium, fin.premium, "премия обеих схем")
    assert_money_equal(own.premium, REF_PREMIUM, "премия обеих схем против 6.3")

    assert_money_equal(
        fin.out_of_pocket - own.out_of_pocket,
        REF_INSURANCE_OVERPAY,
        "переплата за страховку в кредит (ключевая сверка 6.3)",
    )
    assert_money_equal(
        fin.interest - own.interest,
        REF_INSURANCE_OVERPAY,
        "разница процентов двух схем — проценты на саму премию",
    )


# ------------------------------------------------------- 5. вырожденные случаи


def test_zero_insurance_rate_makes_schemes_identical(ref_base_schedule) -> None:
    """Тариф `0 %`: премия `0.00`, обе схемы вырождаются в один и тот же кредит.

    Раздел 3.5: при `ρ = 0` премия равна `money(S_req · 0) = 0.00`, тело схемы
    «в кредит» совпадает с `S_req`, значит оба графика обязаны совпасть с примером
    6.1, а нагрузка на карман — с выплатами банку. Премия обязана быть записана
    как `0.00` с двумя знаками (И-8), а не как `0`.
    """
    own = own_funds("1000000.00", "0.12", 12, "0.00")
    fin = financed("1000000.00", "0.12", 12, "0.00")

    assert_money_equal(own.premium, ZERO, "премия при нулевом тарифе, схема «из своих средств»")
    assert_money_equal(fin.premium, ZERO, "премия при нулевом тарифе, схема «в кредит»")
    assert_money_equal(fin.loan_body, REF_REQUESTED, "тело кредита при нулевом тарифе")
    assert_schedule_equal(own.schedule, ref_base_schedule)
    assert_schedule_equal(fin.schedule, ref_base_schedule)
    assert_money_equal(
        own.out_of_pocket, REF_OWN_PAID_TO_BANK, "нагрузка на карман при нулевом тарифе, 3a"
    )
    assert_money_equal(
        fin.out_of_pocket, REF_OWN_PAID_TO_BANK, "нагрузка на карман при нулевом тарифе, 3b"
    )
    assert_money_equal(
        fin.out_of_pocket - own.out_of_pocket, ZERO, "разница схем при нулевом тарифе"
    )


@pytest.mark.parametrize("scheme", sorted(SCHEMES), ids=sorted(SCHEMES))
def test_negative_insurance_rate_rejected(scheme: str) -> None:
    """Отрицательный тариф отвергается `ValueError`.

    Раздел 5 спеки строит вырожденные случаи на предусловиях `S_req > 0` и `r ≥ 0`;
    отрицательный тариф из той же семьи: он даёт отрицательную премию, ломает И-7
    («все величины неотрицательны») и при схеме «в кредит» делает тело меньше
    запрошенной суммы. Молча посчитанная скидка вместо страховки — худший
    из возможных исходов, поэтому ожидается отказ на входе, а не расчёт.
    """
    with pytest.raises(ValueError):
        SCHEMES[scheme]("1000000.00", "0.12", 12, "-0.05")


def test_negative_insurance_rate_rejected_by_premium_function() -> None:
    """`insurance_premium` тоже отвергает отрицательный тариф — раздел 5.

    Проверка отдельно от схем: если валидация живёт только в обёртках,
    прямой вызов формулы 3.5 останется дырой.
    """
    with pytest.raises(ValueError):
        insurance_premium(REF_REQUESTED, Decimal("-0.05"))


@pytest.mark.parametrize(
    "requested", ["0.00", "-0.01", "-1000000.00"], ids=["ноль", "минус-копейка", "минус-миллион"]
)
@pytest.mark.parametrize("scheme", sorted(SCHEMES), ids=sorted(SCHEMES))
def test_non_positive_requested_amount_rejected(scheme: str, requested: str) -> None:
    """Нулевая и отрицательная запрошенная сумма отвергаются `ValueError` — раздел 5.

    Спека отдельно оговаривает, что пустой график не является допустимым
    результатом при `S = 0`: это не кредит, а отсутствие кредита, и молчаливый
    пустой график маскирует дефект вызывающего кода.
    """
    with pytest.raises(ValueError):
        SCHEMES[scheme](requested, "0.12", 12, "0.05")


@pytest.mark.parametrize("scheme", sorted(SCHEMES), ids=sorted(SCHEMES))
def test_negative_annual_rate_rejected_with_insurance(scheme: str) -> None:
    """Отрицательная годовая ставка отвергается и в сценарии со страховкой — раздел 5.

    Предусловие `r ≥ 0` не отменяется наличием страховки: обе схемы обязаны
    отказать на входе, а не построить график с отрицательными процентами.
    """
    with pytest.raises(ValueError):
        SCHEMES[scheme]("1000000.00", "-0.01", 12, "0.05")


@pytest.mark.parametrize("months", [0, -1], ids=["ноль-месяцев", "минус-месяц"])
@pytest.mark.parametrize("scheme", sorted(SCHEMES), ids=sorted(SCHEMES))
def test_non_positive_term_rejected_with_insurance(scheme: str, months: int) -> None:
    """Срок меньше одного месяца отвергается `ValueError`.

    Раздел 2 спеки: `n` — целое `≥ 1`. При `n = 0` вырожденная формула 3.2
    (`A = money(S / n)`) вдобавок делит на ноль.
    """
    with pytest.raises(ValueError):
        SCHEMES[scheme]("1000000.00", "0.12", months, "0.05")


def test_zero_annual_rate_with_insurance() -> None:
    """Ставка `0 %` вместе со страховкой — раздел 5 плюс раздел 3.5.

    Кредит `120 000.00 / 0 % / 12 мес`, тариф `5 %`, премия `6 000.00`.
    По формуле 3.2 при `i = 0` платёж равен `money(S / n)`: из своих средств
    `10 000.00`, в кредит — `money(126 000.00 / 12) = 10 500.00`. Проценты
    всюду нулевые, а значит проценты на премию не начисляются и обе схемы
    обходятся заёмщику одинаково — `126 000.00`. Это тот случай, где ключевая
    разница раздела 6.3 обязана выродиться ровно в ноль.
    """
    own = own_funds("120000.00", "0.00", 12, "0.05")
    fin = financed("120000.00", "0.00", 12, "0.05")

    assert_money_equal(own.premium, Decimal("6000.00"), "премия при нулевой ставке")
    assert_money_equal(fin.premium, Decimal("6000.00"), "премия при нулевой ставке, 3b")
    assert_money_equal(own.loan_body, Decimal("120000.00"), "тело кредита 3a при нулевой ставке")
    assert_money_equal(fin.loan_body, Decimal("126000.00"), "тело кредита 3b при нулевой ставке")

    assert len(own.schedule) == 12, f"график 3a: строк {len(own.schedule)}, спека требует 12"
    assert len(fin.schedule) == 12, f"график 3b: строк {len(fin.schedule)}, спека требует 12"
    for row in own.schedule:
        assert_money_equal(row.payment, Decimal("10000.00"), f"месяц {row.number}, платёж 3a")
        assert_money_equal(row.interest, ZERO, f"месяц {row.number}, проценты 3a")
    for row in fin.schedule:
        assert_money_equal(row.payment, Decimal("10500.00"), f"месяц {row.number}, платёж 3b")
        assert_money_equal(row.interest, ZERO, f"месяц {row.number}, проценты 3b")

    assert_money_equal(own.interest, ZERO, "проценты 3a при нулевой ставке")
    assert_money_equal(fin.interest, ZERO, "проценты 3b при нулевой ставке")
    assert_money_equal(own.out_of_pocket, Decimal("126000.00"), "из кармана 3a при нулевой ставке")
    assert_money_equal(fin.out_of_pocket, Decimal("126000.00"), "из кармана 3b при нулевой ставке")
    assert_money_equal(
        fin.out_of_pocket - own.out_of_pocket,
        ZERO,
        "при нулевой ставке проценты на премию не начисляются, схемы обязаны совпасть",
    )


def test_single_month_term_with_insurance() -> None:
    """Срок `1 месяц` вместе со страховкой — раздел 5 плюс раздел 3.4.

    Кредит `100 000.00 / 12 % / 1 мес`, тариф `5 %`, премия `5 000.00`.
    Единственный платёж он же последний и балансирующий: `A_1 = money(S + money(S·i))`.
    Из своих средств — `101 000.00` (строка раздела 5 спеки дословно), в кредит
    тело `105 000.00` даёт `I_1 = 1 050.00` и `A_1 = 106 050.00`. Разница схем
    равна процентам ровно на премию: `money(5 000.00 · 0.01) = 50.00`.
    """
    own = own_funds("100000.00", "0.12", 1, "0.05")
    fin = financed("100000.00", "0.12", 1, "0.05")

    assert len(own.schedule) == 1, f"график 3a на 1 месяц: строк {len(own.schedule)}"
    assert len(fin.schedule) == 1, f"график 3b на 1 месяц: строк {len(fin.schedule)}"

    own_row, fin_row = own.schedule[0], fin.schedule[0]
    assert_money_equal(own_row.payment, Decimal("101000.00"), "единственный платёж 3a")
    assert_money_equal(own_row.interest, Decimal("1000.00"), "проценты месяца 1, 3a")
    assert_money_equal(own_row.principal, Decimal("100000.00"), "тело месяца 1, 3a")
    assert_money_equal(own_row.balance, ZERO, "остаток после единственного платежа 3a")

    assert_money_equal(fin.loan_body, Decimal("105000.00"), "тело кредита 3b на 1 месяц")
    assert_money_equal(fin_row.payment, Decimal("106050.00"), "единственный платёж 3b")
    assert_money_equal(fin_row.interest, Decimal("1050.00"), "проценты месяца 1, 3b")
    assert_money_equal(fin_row.principal, Decimal("105000.00"), "тело месяца 1, 3b")
    assert_money_equal(fin_row.balance, ZERO, "остаток после единственного платежа 3b")

    assert_money_equal(own.out_of_pocket, Decimal("106000.00"), "из кармана 3a на 1 месяц")
    assert_money_equal(fin.out_of_pocket, Decimal("106050.00"), "из кармана 3b на 1 месяц")
    assert_money_equal(
        fin.out_of_pocket - own.out_of_pocket,
        Decimal("50.00"),
        "разница схем на 1 месяц — проценты на премию money(5 000.00 · 0.01)",
    )


def test_kopeck_requested_amount_gives_zero_premium() -> None:
    """Копеечная сумма: премия округляется в `0.00`, схемы совпадают.

    `S_req = 0.01`, тариф `5 %` → `money(0.0005) = 0.00`: премия меньше половины
    копейки исчезает при округлении раздела 1. Тело схемы «в кредит» тогда равно
    `0.01`, оба графика одинаковы, кредит закрывается за один месяц с остатком
    `0.00`. Срок взят в один месяц намеренно: на длинном сроке копеечное тело
    попадает в неамортизируемый случай раздела 5.1, и это отдельная проверка
    другого модуля.
    """
    own = own_funds("0.01", "0.12", 1, "0.05")
    fin = financed("0.01", "0.12", 1, "0.05")

    assert_money_equal(own.premium, ZERO, "премия с копеечной суммы")
    assert_money_equal(fin.premium, ZERO, "премия с копеечной суммы, 3b")
    assert_money_equal(own.loan_body, Decimal("0.01"), "тело кредита 3a при копеечной сумме")
    assert_money_equal(fin.loan_body, Decimal("0.01"), "тело кредита 3b при копеечной сумме")
    assert_money_equal(own.schedule[0].payment, Decimal("0.01"), "платёж 3a при копеечной сумме")
    assert_money_equal(fin.schedule[0].payment, Decimal("0.01"), "платёж 3b при копеечной сумме")
    assert_money_equal(own.schedule[0].interest, ZERO, "проценты 3a при копеечной сумме")
    assert_money_equal(own.schedule[0].balance, ZERO, "остаток 3a при копеечной сумме")
    assert_money_equal(fin.schedule[0].balance, ZERO, "остаток 3b при копеечной сумме")
    assert_money_equal(own.out_of_pocket, Decimal("0.01"), "из кармана 3a при копеечной сумме")
    assert_money_equal(fin.out_of_pocket, Decimal("0.01"), "из кармана 3b при копеечной сумме")


def test_kopeck_premium_is_carried_by_both_schemes() -> None:
    """Премия ровно в копейку доходит до заёмщика в обеих схемах.

    `S_req = 0.20`, тариф `5 %` → `money(0.01) = 0.01`. Из своих средств премия
    добавляется к выплатам банку (`0.20 + 0.01`), в кредит — поднимает тело
    до `0.21`. Обе схемы дают одинаковый итог `0.21`: на копеечное тело
    процентов не набегает, но потерять саму копейку нельзя.
    """
    own = own_funds("0.20", "0.12", 1, "0.05")
    fin = financed("0.20", "0.12", 1, "0.05")

    assert_money_equal(own.premium, Decimal("0.01"), "копеечная премия 3a")
    assert_money_equal(fin.premium, Decimal("0.01"), "копеечная премия 3b")
    assert_money_equal(own.loan_body, Decimal("0.20"), "тело кредита 3a при копеечной премии")
    assert_money_equal(fin.loan_body, Decimal("0.21"), "тело кредита 3b при копеечной премии")
    assert_money_equal(own.paid_to_bank, Decimal("0.20"), "выплачено банку 3a при копеечной премии")
    assert_money_equal(fin.paid_to_bank, Decimal("0.21"), "выплачено банку 3b при копеечной премии")
    assert_money_equal(own.out_of_pocket, Decimal("0.21"), "из кармана 3a при копеечной премии")
    assert_money_equal(fin.out_of_pocket, Decimal("0.21"), "из кармана 3b при копеечной премии")


# --------------------------------------------------------- 4. инварианты


#: Сценарии для инвариантов раздела 4: эталон 6.3, обе вырожденные границы
#: (ставка `0 %`, срок `1 месяц`), нулевой тариф, копеечное тело и длинный срок
#: с непериодической месячной ставкой (`9.5 % / 12 = 0.0079166…`).
INVARIANT_CASES = [
    ("1000000.00", "0.12", 12, "0.05"),
    ("120000.00", "0.00", 12, "0.05"),
    ("100000.00", "0.12", 1, "0.05"),
    ("1000000.00", "0.12", 12, "0.00"),
    ("0.20", "0.12", 1, "0.05"),
    ("500000.00", "0.095", 60, "0.01"),
]


@pytest.mark.parametrize(
    ("requested", "annual_rate", "months", "insurance_rate"),
    INVARIANT_CASES,
    ids=["эталон-6.3", "ставка-0%", "срок-1мес", "тариф-0%", "копейки", "9.5%/60мес"],
)
@pytest.mark.parametrize("scheme", sorted(SCHEMES), ids=sorted(SCHEMES))
def test_invariants_hold_for_insurance_schedules(
    scheme: str, requested: str, annual_rate: str, months: int, insurance_rate: str
) -> None:
    """Инварианты раздела 4 держатся на обоих графиках со страховкой.

    Спека требует применять И-1 … И-10 ко всем эталонным примерам и к вырожденным
    случаям без исключений. Тело кредита для проверки И-1 берётся из самого
    результата — и отдельным тестом ниже проверяется, что для схемы «в кредит»
    оно не равно запрошенной сумме.
    """
    result = SCHEMES[scheme](requested, annual_rate, months, insurance_rate)
    assert_schedule_invariants(
        result.schedule,
        result.loan_body,
        months,
        f"{scheme}: {requested} / {annual_rate} / {months} мес / тариф {insurance_rate}",
    )
    assert_result_consistent(result, f"{scheme}: {requested} / {annual_rate} / {months} мес")


def test_principal_sum_equals_loan_body_not_requested_amount() -> None:
    """И-1 для схемы «в кредит»: сумма тел равна `S_req + p`, а не `S_req`.

    Самое опасное место инварианта И-1 при страховке в кредит: `1 050 000.00`
    против `1 000 000.00`. Тест, сверяющий сумму тел с запрошенной суммой,
    прошёл бы на схеме «из своих средств» и промолчал бы там, где премия
    потерялась. Поэтому здесь проверяется и равенство телу, и неравенство
    запрошенной сумме.
    """
    fin = financed("1000000.00", "0.12", 12, "0.05")
    body_sum = sum_money(row.principal + row.prepayment for row in fin.schedule)

    assert_money_equal(body_sum, REF_FINANCED_BODY, "И-1, сумма тел схемы «в кредит»")
    assert_money_equal(body_sum, fin.loan_body, "И-1, сумма тел против тела кредита")
    assert body_sum != REF_REQUESTED, (
        f"сумма тел {body_sum} совпала с запрошенной суммой {REF_REQUESTED}: "
        f"премия не попала в тело кредита, хотя раздел 3.5 требует S = S_req + p"
    )

    own = own_funds("1000000.00", "0.12", 12, "0.05")
    own_body_sum = sum_money(row.principal + row.prepayment for row in own.schedule)
    assert_money_equal(own_body_sum, REF_REQUESTED, "И-1, сумма тел схемы «из своих средств»")


# ---------------------------------------------------- Н-1: закрытие графика


@pytest.mark.parametrize("scheme", sorted(SCHEMES), ids=sorted(SCHEMES))
def test_insurance_schedule_closes_without_kopeck_tail(scheme: str) -> None:
    """Находка Н-1: график со страховкой закрывается в ноль без хвоста в копейку.

    Раздел 3.4 спеки: платёж считается последним не только когда перекрывает
    остаток, но и когда исчерпан срок (`k = n`). Без условия (б) после 12-го
    платежа остаётся `0.01` и появляется фиктивный 13-й платёж на копейку —
    именно так это и проявилось при подготовке спеки. Проверяется ровно 12 строк,
    отсутствие тринадцатой, нулевой остаток без допуска и балансирующий последний
    платёж по И-3.

    Сторона отклонения последнего платежа не фиксируется: в примере 6.3b он
    меньше регулярного (`93 291.22` против `93 291.23`), а в примере 6.2b —
    больше. Раздел 6.2 прямо предупреждает, что тест «последний платёж всегда
    меньше» ложно упадёт.
    """
    result = SCHEMES[scheme]("1000000.00", "0.12", 12, "0.05")
    schedule = result.schedule

    assert len(schedule) == 12, (
        f"{scheme}: в графике {len(schedule)} строк вместо 12 — "
        f"похоже на фиктивный платёж-хвост из находки Н-1"
    )
    assert schedule[-1].balance == ZERO, (
        f"{scheme}: остаток после последнего платежа {schedule[-1].balance}, "
        f"И-2 требует ровно 0.00 без допуска"
    )
    assert_money_equal(
        schedule[-1].payment,
        schedule[-2].balance + schedule[-1].interest,
        f"{scheme}: И-3, балансирующий последний платёж",
    )
    tail = [row.number for row in schedule if row.payment == Decimal("0.01")]
    assert not tail, (
        f"{scheme}: копеечный платёж в месяцах {tail} — это хвост округления "
        f"из находки Н-1, а не законная строка графика"
    )
    deviating = [row.number for row in schedule[:-1] if row.payment != schedule[0].payment]
    assert not deviating, (
        f"{scheme}: И-3, кроме последнего от регулярного платежа отличаются месяцы {deviating}"
    )


# ------------------------------------------------------------ 1. запрет float


#: Вызовы с `float` ровно в одном аргументе — по одному на каждую позицию
#: каждой публичной функции модуля страховки.
FLOAT_CASES = {
    "premium/сумма": lambda: insurance_premium(1000000.0, Decimal("0.05")),
    "premium/тариф": lambda: insurance_premium(Decimal("1000000.00"), 0.05),
    "own_funds/сумма": lambda: loan_with_own_funds_insurance(
        1000000.0, Decimal("0.12"), 12, Decimal("0.05")
    ),
    "own_funds/ставка": lambda: loan_with_own_funds_insurance(
        Decimal("1000000.00"), 0.12, 12, Decimal("0.05")
    ),
    "own_funds/срок": lambda: loan_with_own_funds_insurance(
        Decimal("1000000.00"), Decimal("0.12"), 12.0, Decimal("0.05")
    ),
    "own_funds/тариф": lambda: loan_with_own_funds_insurance(
        Decimal("1000000.00"), Decimal("0.12"), 12, 0.05
    ),
    "financed/сумма": lambda: loan_with_financed_insurance(
        1000000.0, Decimal("0.12"), 12, Decimal("0.05")
    ),
    "financed/ставка": lambda: loan_with_financed_insurance(
        Decimal("1000000.00"), 0.12, 12, Decimal("0.05")
    ),
    "financed/срок": lambda: loan_with_financed_insurance(
        Decimal("1000000.00"), Decimal("0.12"), 12.0, Decimal("0.05")
    ),
    "financed/тариф": lambda: loan_with_financed_insurance(
        Decimal("1000000.00"), Decimal("0.12"), 12, 0.05
    ),
}


@pytest.mark.parametrize("case", list(FLOAT_CASES), ids=list(FLOAT_CASES))
def test_float_arguments_rejected(case: str) -> None:
    """`float` в любом аргументе отвергается `TypeError` — раздел 1 спеки.

    Деньги создаются только из строк: `Decimal(0.05)` — это
    `0.05000000000000000277…`, и такой дефект не виден до сверки последней копейки.
    Ошибка обязана возникать в точке передачи аргумента, а не превращаться
    в чуть-чуть неправильную премию.
    """
    with pytest.raises(TypeError):
        FLOAT_CASES[case]()


# ------------------------------------------------------------ 8.1. границы области


def test_annual_monthly_and_discount_schemes_are_out_of_scope(spec_text: str) -> None:
    """Отметка об области: три схемы страхования сознательно не реализованы.

    Раздел 8.1 спеки перечисляет ежегодную страховку, ежемесячную страховку
    и скидку к ставке за страховку как не реализованные по решению. Тестов
    на их поведение в этом модуле нет намеренно — падающий тест на нереализованное
    был бы не находкой, а шумом. Этот тест сторожит саму границу области:
    если строки из раздела 8.1 исчезнут, значит область изменилась и регресс
    надо расширять, а не молча считать пробел покрытым.

    Спека сама называет скидку к ставке за страховку главным пробелом регресса
    и самым вероятным источником дефектов в проде — это стоит держать в отчёте.
    """
    start = spec_text.index("### 8.1.")
    section = spec_text[start : spec_text.index("### 8.2.")]
    for missing_feature in (
        "Ежегодная страховка",
        "Ежемесячная страховка",
        "Скидка к ставке за страховку",
    ):
        assert missing_feature in section, (
            f"раздел 8.1 спеки больше не объявляет «{missing_feature}» вне области — "
            f"либо схема реализована и требует тестов, либо спека изменилась"
        )
