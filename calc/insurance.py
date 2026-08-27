"""Страховка: единовременная премия из своих средств и единовременная премия в кредит.

Реализует раздел 3.5 спеки. Других схем нет по решению из раздела 1: ежегодная
и ежемесячная страховка, а также скидка к ставке за страховку не реализуются
и перечислены в разделе 8.1 спеки как сознательно не покрытые.

Премия считается от **запрошенной** суммы `S_req`, а не от тела кредита — иначе
при схеме «в кредит» получается круговая зависимость. Это решение спеки, у него
есть цена: часть банков считает премию от полного тела, и тогда числа эталонного
примера 6.3b другие (раздел 8.2 спеки).
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

from calc.annuity import ScheduleRow, build_schedule, total_interest, total_paid
from calc.money import money, reject_float

__all__ = [
    "InsuranceResult",
    "insurance_premium",
    "loan_with_own_funds_insurance",
    "loan_with_financed_insurance",
]


class InsuranceResult(NamedTuple):
    """Результат расчёта кредита со страховкой.

    График здесь — обычный список `ScheduleRow` из шести полей (раздел 4.3 спеки);
    остальные поля нужны, чтобы выразить денежный поток заёмщика из раздела 3.5:
    при премии из своих средств она платится вне графика, при премии в кредит —
    внутри него.
    """

    #: Схема страхования: `"own_funds"` или `"financed"`.
    scheme: str
    #: `p` — страховая премия.
    premium: Decimal
    #: `S` — тело кредита, на которое построен график.
    loan_body: Decimal
    #: График погашения.
    schedule: list[ScheduleRow]
    #: Сумма всех платежей банку по графику.
    paid_to_bank: Decimal
    #: Проценты по графику.
    interest: Decimal
    #: Полная нагрузка на заёмщика: платежи банку плюс премия, если та вне графика.
    out_of_pocket: Decimal


def insurance_premium(requested_amount: Decimal, insurance_rate: Decimal) -> Decimal:
    """Рассчитать единовременную страховую премию.

    Вход: запрошенная заёмщиком сумма `S_req` и тариф страхования `ρ` долей единицы
    (`Decimal("0.05")` для 5 %).
    Выход: премия `p = money(S_req · ρ)`, `Decimal` с 2 знаками.

    Спека: раздел 3.5. Тариф `0` допустим и даёт премию `0.00` — тогда обе схемы
    совпадают. Отрицательный тариф отвергается: инвариант И-7 требует неотрицательных
    денежных величин.
    """
    reject_float(requested_amount, "запрошенная сумма")
    reject_float(insurance_rate, "тариф страхования")
    if not isinstance(requested_amount, Decimal):
        raise TypeError(
            f"запрошенная сумма: ожидается Decimal, получен {type(requested_amount).__name__}"
        )
    if not isinstance(insurance_rate, Decimal):
        raise TypeError(
            f"тариф страхования: ожидается Decimal, получен {type(insurance_rate).__name__}"
        )
    if requested_amount <= 0:
        raise ValueError(
            f"запрошенная сумма должна быть строго положительной, получено {requested_amount}; "
            f"раздел 5 спеки"
        )
    if insurance_rate < 0:
        raise ValueError(
            f"тариф страхования не может быть отрицательным, получено {insurance_rate}; "
            f"инвариант И-7 спеки"
        )
    return money(requested_amount * insurance_rate)


def loan_with_own_funds_insurance(
    requested_amount: Decimal,
    annual_rate: Decimal,
    months: int,
    insurance_rate: Decimal,
) -> InsuranceResult:
    """Рассчитать кредит со страховкой, оплаченной из своих средств.

    Вход: запрошенная сумма `S_req`, годовая ставка `r`, срок `n`, тариф `ρ`.
    Выход: `InsuranceResult`; тело кредита равно `S_req`, премия в график не входит.

    Спека: раздел 3.5, строка «Единовременная из своих средств». Премия платится
    отдельно и процентами не обрастает, поэтому полная нагрузка складывается
    как сумма платежей плюс премия.

    В эталонном примере 6.3a график совпадает с примером 6.1: `1 066 185.45` банку
    плюс премия `50 000.00`, итого `1 116 185.45`.
    """
    premium = insurance_premium(requested_amount, insurance_rate)
    schedule = build_schedule(requested_amount, annual_rate, months)
    paid = total_paid(schedule)
    return InsuranceResult(
        scheme="own_funds",
        premium=premium,
        loan_body=requested_amount,
        schedule=schedule,
        paid_to_bank=paid,
        interest=total_interest(schedule),
        out_of_pocket=money(paid + premium),
    )


def loan_with_financed_insurance(
    requested_amount: Decimal,
    annual_rate: Decimal,
    months: int,
    insurance_rate: Decimal,
) -> InsuranceResult:
    """Рассчитать кредит со страховкой, включённой в тело кредита.

    Вход: запрошенная сумма `S_req`, годовая ставка `r`, срок `n`, тариф `ρ`.
    Выход: `InsuranceResult`; тело кредита равно `S_req + p`, премия внутри графика.

    Спека: раздел 3.5, строка «Единовременная в кредит». На саму премию начисляются
    проценты, поэтому эта схема дороже предыдущей при одинаковой премии.

    В эталонном примере 6.3b тело `1 050 000.00`, платёж `93 291.23`, итого
    `1 119 494.75` — на `3 309.30` дороже схемы из своих средств. Эта разница
    и есть проценты на премию; сверять её спека требует отдельно от графиков.
    """
    premium = insurance_premium(requested_amount, insurance_rate)
    body = money(requested_amount + premium)
    schedule = build_schedule(body, annual_rate, months)
    paid = total_paid(schedule)
    return InsuranceResult(
        scheme="financed",
        premium=premium,
        loan_body=body,
        schedule=schedule,
        paid_to_bank=paid,
        interest=total_interest(schedule),
        out_of_pocket=paid,
    )
