"""Аннуитетный платёж и построение графика погашения.

Реализует разделы 3.1–3.4 спеки (месячная ставка, аннуитет, разложение платежа,
балансирующий последний платёж), форму строки из раздела 4.3 и предусловия
из разделов 2 и 5.

Досрочное погашение живёт в `calc.prepayment`, страховка — в `calc.insurance`;
оба модуля строят график этой же функцией `build_schedule`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, NamedTuple, Sequence

from calc.money import ZERO, calculation_context, money, reject_float

__all__ = [
    "ScheduleRow",
    "PaymentDoesNotAmortise",
    "ScheduleDidNotTerminate",
    "monthly_rate",
    "annuity_payment",
    "build_schedule",
    "total_interest",
    "total_paid",
    "validate_loan",
]


class PaymentDoesNotAmortise(ValueError):
    """Платёж не покрывает проценты: основная часть не положительна.

    Раздел 5.1 спеки. Случай достижим после округления до копеек на очень малых телах
    кредита: `money(A) == money(B · i)`, основная часть равна нулю, остаток не сдвигается.
    График в таком виде не строится — иначе цикл не заканчивается.
    """


class ScheduleDidNotTerminate(RuntimeError):
    """Число строк графика превысило срок кредита.

    Страховка второго уровня к разделу 5.1 и инварианту И-10: даже если проверка
    основной части почему-то не сработала, цикл обязан упасть по счётчику итераций,
    а не крутиться до таймаута.
    """


class ScheduleRow(NamedTuple):
    """Строка графика погашения — шесть полей, раздел 4.3 спеки.

    Тело и досрочка разделены намеренно: без этого не сверить эталонные таблицы 6.2
    и не проверить инвариант И-1 (`Σ (D_k + E_k) = S`).
    """

    #: `k` — номер платёжного месяца, начиная с 1.
    number: int
    #: `A_k` — платёж месяца; в последней строке балансирующий (раздел 3.4).
    payment: Decimal
    #: `I_k` — процентная часть платежа.
    interest: Decimal
    #: `D_k` — основная часть платежа, **без** досрочки.
    principal: Decimal
    #: `E_k` — фактически применённая досрочка, уже обрезанная по остатку; `0.00`, если её не было.
    prepayment: Decimal
    #: `B_k` — остаток основного долга после платежа и досрочки.
    balance: Decimal


def validate_loan(principal: Decimal, annual_rate: Decimal, months: int) -> None:
    """Проверить предусловия кредита.

    Вход: тело кредита `S`, годовая ставка `r`, срок `n` в месяцах.
    Выход: `None`, если всё допустимо; иначе `ValueError` (или `TypeError` на `float`).

    Спека: раздел 2 (`n ≥ 1`, `r ≥ 0`) и раздел 5 — отрицательная и нулевая сумма,
    а также отрицательная ставка отвергаются на входе, расчёт не выполняется.
    Нулевая ставка допустима и обрабатывается отдельной ветвью в `annuity_payment`.
    """
    reject_float(principal, "сумма кредита")
    reject_float(annual_rate, "годовая ставка")

    if not isinstance(principal, Decimal):
        raise TypeError(f"сумма кредита: ожидается Decimal, получен {type(principal).__name__}")
    if not isinstance(annual_rate, Decimal):
        raise TypeError(f"годовая ставка: ожидается Decimal, получен {type(annual_rate).__name__}")
    if isinstance(months, bool) or not isinstance(months, int):
        raise TypeError(f"срок: ожидается int, получен {type(months).__name__}")

    if principal <= 0:
        raise ValueError(
            f"сумма кредита должна быть строго положительной, получено {principal}; "
            f"раздел 5 спеки: нулевая сумма — это не кредит, а его отсутствие"
        )
    if annual_rate < 0:
        raise ValueError(
            f"годовая ставка не может быть отрицательной, получено {annual_rate}; "
            f"раздел 5 спеки: r < 0 вне области варианта B4"
        )
    if months < 1:
        raise ValueError(f"срок должен быть не меньше 1 месяца, получено {months}")


def monthly_rate(annual_rate: Decimal) -> Decimal:
    """Перевести годовую ставку в месячную.

    Вход: годовая номинальная ставка `r` долей единицы (`Decimal("0.12")` для 12 %).
    Выход: месячная ставка `i = r / 12`, **без** приведения к копейкам.

    Спека: раздел 3.1. База 30/360 — все месяцы равны, поэтому делитель ровно 12
    и фактические дни не участвуют. Результат намеренно длинный: для 9.5 % годовых
    это `0.0079166…`, и в формулу идёт именно оно.
    """
    reject_float(annual_rate, "годовая ставка")
    with calculation_context():
        return annual_rate / Decimal(12)


def annuity_payment(principal: Decimal, annual_rate: Decimal, months: int) -> Decimal:
    """Рассчитать аннуитетный платёж.

    Вход: тело кредита `S`, годовая ставка `r`, срок `n` в месяцах.
    Выход: платёж `A`, округлённый до копеек.

    Спека: раздел 3.2. При `i > 0` — `A = money(S · i · (1+i)^n / ((1+i)^n − 1))`,
    множитель `(1+i)^n` не округляется. При `i = 0` формула вырождается делением
    на ноль, поэтому используется отдельная ветвь `A = money(S / n)` (раздел 5,
    строка «Ставка 0 %»).
    """
    validate_loan(principal, annual_rate, months)
    with calculation_context():
        rate = monthly_rate(annual_rate)
        if rate == 0:
            return money(principal / Decimal(months))
        growth = (Decimal(1) + rate) ** months
        return money(principal * rate * growth / (growth - Decimal(1)))


def build_schedule(
    principal: Decimal,
    annual_rate: Decimal,
    months: int,
    *,
    prepayments: Mapping[int, Decimal] | None = None,
    keep_term: bool = True,
    recalculate_payment: bool = False,
) -> list[ScheduleRow]:
    """Построить график погашения.

    Вход:
      * `principal` — тело кредита `S`, строго положительное;
      * `annual_rate` — годовая ставка `r`, неотрицательная;
      * `months` — срок `n`, целое не меньше 1;
      * `prepayments` — досрочки вида `{номер месяца: сумма}`, необязательно;
      * `keep_term` — срок зафиксирован на `n` (см. ниже);
      * `recalculate_payment` — пересчитывать платёж после досрочки.

    Выход: список `ScheduleRow`. Последняя строка всегда закрывает долг в ноль.

    Спека: разделы 3.3 (разложение платежа), 3.4 (балансирующий платёж),
    3.6 (порядок списания досрочки), 4.3 (форма строки), 5 и 5.1 (вырожденные случаи).

    Три сочетания флагов соответствуют трём режимам спеки:
      * `keep_term=True,  recalculate_payment=False` — обычный аннуитет без досрочек;
      * `keep_term=False, recalculate_payment=False` — досрочка с сокращением срока;
      * `keep_term=True,  recalculate_payment=True`  — досрочка с уменьшением платежа.

    Прямые вызовы обычно не передают ни досрочек, ни флагов: именованные точки входа
    для досрочки — в `calc.prepayment`.

    Про `keep_term`. Это условие (б) правила 3.4: при фиксированном сроке платёж месяца
    `n` обязан добрать остаток, иначе после округления остаётся хвост в копейку
    и появляется фиктивная лишняя строка (находка Н-1, разделы 3.4 и 6.2 спеки).
    При сокращении срока условие не применяется — срок там свободен, и последняя
    строка может оказаться сколь угодно малой, вплоть до `0.01` (находка Н-3, раздел 4.2).

    Ошибки: `ValueError` на недопустимых входных данных, `PaymentDoesNotAmortise`
    при неамортизируемом платеже (раздел 5.1), `ScheduleDidNotTerminate`, если строк
    оказалось больше срока.
    """
    validate_loan(principal, annual_rate, months)
    if recalculate_payment and not keep_term:
        raise ValueError(
            "recalculate_payment=True требует keep_term=True: по разделу 3.6 спеки "
            "уменьшение платежа сохраняет срок кредита"
        )
    schedule_prepayments = _validate_prepayments(prepayments, months)

    with calculation_context():
        rate = monthly_rate(annual_rate)
        payment = annuity_payment(principal, annual_rate, months)
        balance = principal
        rows: list[ScheduleRow] = []
        number = 0

        while balance > 0:
            number += 1
            if number > months:
                raise ScheduleDidNotTerminate(
                    f"график не закрылся за {months} месяцев: остаток {balance} "
                    f"на шаге {number}; инвариант И-10 нарушен"
                )

            interest = money(balance * rate)
            is_final = (payment - interest >= balance) or (keep_term and number >= months)
            current = money(balance + interest) if is_final else payment

            principal_part = current - interest
            if principal_part <= 0:
                raise PaymentDoesNotAmortise(
                    f"платёж {current} не покрывает проценты {interest} на шаге {number}: "
                    f"основная часть {principal_part}, остаток {balance} не уменьшается "
                    f"(раздел 5.1 спеки)"
                )

            balance -= principal_part
            applied = min(schedule_prepayments.get(number, ZERO), balance)
            balance -= applied

            rows.append(ScheduleRow(number, current, interest, principal_part, applied, balance))

            if recalculate_payment and applied > 0 and balance > 0:
                payment = annuity_payment(balance, annual_rate, months - number)

    return rows


def total_paid(schedule: Sequence[ScheduleRow]) -> Decimal:
    """Сумма всех выплат по графику, включая досрочки.

    Вход: график.
    Выход: `Σ (A_k + E_k)`, `Decimal` с 2 знаками.

    Спека: блоки «Всего выплачено» в разделе 6 и инвариант И-9.
    """
    return money(sum((row.payment + row.prepayment for row in schedule), ZERO))


def total_interest(schedule: Sequence[ScheduleRow]) -> Decimal:
    """Сумма процентов по графику.

    Вход: график.
    Выход: `Σ I_k`, `Decimal` с 2 знаками.

    Спека: блоки «Проценты» в разделе 6; используется для сравнения видов досрочки
    в разделе 6.2.
    """
    return money(sum((row.interest for row in schedule), ZERO))


def _validate_prepayments(
    prepayments: Mapping[int, Decimal] | None, months: int
) -> dict[int, Decimal]:
    """Проверить и нормализовать досрочки.

    Вход: отображение `{номер месяца: сумма}` или `None`; срок кредита.
    Выход: словарь с суммами, приведёнными к копейкам.

    Спека: раздел 2 (`k` пробегает `1 … n`) и инвариант И-7 (`E_k ≥ 0`).
    Досрочка на месяц, до которого график не дожил из-за сокращения срока,
    просто не применяется — это не ошибка.
    """
    if not prepayments:
        return {}

    normalised: dict[int, Decimal] = {}
    for number, amount in prepayments.items():
        if isinstance(number, bool) or not isinstance(number, int):
            raise TypeError(f"номер месяца досрочки: ожидается int, получен {type(number).__name__}")
        if not 1 <= number <= months:
            raise ValueError(
                f"номер месяца досрочки вне срока кредита: {number}, допустимо 1..{months}"
            )
        reject_float(amount, f"досрочка месяца {number}")
        if not isinstance(amount, Decimal):
            raise TypeError(
                f"досрочка месяца {number}: ожидается Decimal, получен {type(amount).__name__}"
            )
        if amount < 0:
            raise ValueError(
                f"досрочка месяца {number} не может быть отрицательной, получено {amount}; "
                f"инвариант И-7 спеки требует E_k ≥ 0"
            )
        normalised[number] = money(amount)
    return normalised
