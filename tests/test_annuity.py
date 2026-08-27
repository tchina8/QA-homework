"""Регресс аннуитета: ставка, срок, график платежей и инварианты.

Источник ожиданий — `docs/spec.md`, и только он. Ни одно число в этом модуле не взято
из фактического поведения `calc/`: эталонные графики и итоги разбираются из самой спеки
(фикстуры `conftest.py`), формулы разделов 3.1–3.4 воспроизводятся в тесте независимо
от реализации, а вырожденные случаи взяты построчно из таблицы раздела 5.

Зона этого файла — ставка, срок, график, инварианты. Досрочное погашение (раздел 3.6,
примеры 6.2, находка Н-3) и страховка (раздел 3.5, пример 6.3) проверяются другими
модулями регресса.

Покрытые находки раздела 7:
* Н-1 — балансирующий последний платёж, условие `k = n` при фиксированном сроке
  (раздел 3.4(б)): без него в графике появляется хвостовая строка на копейку;
* Н-2 — совпадение соседних ненулевых процентов, кредит `100.00 / 0.1 % / 12 мес`
  (раздел 4.1): И-4 обязано быть нестрогим.

Отдельно про раздел 5.1 («платёж не покрывает проценты»): такой вызов обязан падать
ошибкой по счётчику итераций внутри построителя, а не висеть. Чтобы упавшая защита
не подвешивала весь прогон, вызов выполняется в отдельном потоке с дедлайном —
дедлайн здесь не ожидание теста, а страховка: его срабатывание и есть сообщение
о том, что защиты в коде нет.
"""

from __future__ import annotations

import re
import threading
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Callable, NamedTuple, Sequence

import pytest

from conftest import (
    ZERO,
    SpecRow,
    assert_money_equal,
    assert_schedule_equal,
    sum_money,
    to_decimal,
)

from calc.annuity import (
    PaymentDoesNotAmortise,
    ScheduleDidNotTerminate,
    ScheduleRow,
    annuity_payment,
    build_schedule,
    monthly_rate,
    total_interest,
    total_paid,
    validate_loan,
)
from calc.money import reject_float

#: Копейка — минимальная денежная единица модели (раздел 2 спеки: деньги в 2 знаках).
KOPECK = Decimal("0.01")

#: Точность арифметики *теста*. Спека (раздел 3.1–3.2) требует, чтобы промежуточные
#: величины не квантовались; здесь берётся заведомо избыточная точность, чтобы
#: ожидание теста не зависело от внутренней точности реализации.
SPEC_PRECISION = 60

#: Сколько секунд ждать построитель графика в тестах раздела 5.1, прежде чем считать,
#: что защиты от зацикливания нет. Корректная реализация падает мгновенно.
NON_TERMINATION_DEADLINE_SECONDS = 15.0


# --------------------------------------------------------------- воспроизведение спеки


def spec_money(value: Decimal) -> Decimal:
    """Округление денег по разделу 1.1 спеки, воспроизведённое независимо от кода.

    Вход: точное `Decimal`-значение. Выход: `Decimal` с 2 знаками, `ROUND_HALF_UP`.

    Намеренно не импортируется `calc.money.money`: ожидание теста не должно проходить
    через ту же функцию, которую тест проверяет. Тело взято дословно из раздела 1.1.
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def spec_annuity_payment(principal: Decimal, rate: Decimal, months: int) -> Decimal:
    """Аннуитетный платёж по формуле раздела 3.2 спеки.

    Вход: тело кредита, **месячная** ставка `i`, срок в месяцах.
    Выход: `A`, округлённый один раз в конце.

    При `i > 0` считается `S · i(1+i)^n / ((1+i)^n − 1)`, при `i = 0` — вырожденная
    ветка `S / n`. Степень и частное вычисляются с запасом точности и не квантуются:
    раздел 3.2 требует округлять только результат.
    """
    with localcontext() as context:
        context.prec = SPEC_PRECISION
        if rate == 0:
            return spec_money(principal / months)
        growth = (Decimal(1) + rate) ** months
        return spec_money(principal * rate * growth / (growth - Decimal(1)))


def _spec_section(spec_text: str, anchor: str) -> str:
    """Вернуть текст раздела спеки, начиная с якоря и до следующего заголовка.

    Вход: полный текст спеки и якорь вида `"### 6.1."`.
    Выход: подстрока раздела.
    """
    start = spec_text.index(anchor)
    rest = spec_text[start + len(anchor):]
    following = re.search(r"\n#{2,4} ", rest)
    return rest[: following.start()] if following else rest


def _spec_code_blocks(section: str) -> list[str]:
    """Вернуть содержимое всех блоков ``` из текста раздела.

    Вход: текст раздела. Выход: список тел блоков.
    """
    return re.findall(r"```[a-z]*\n(.*?)```", section, flags=re.DOTALL)


def spec_totals(spec_text: str, anchor: str) -> dict[str, Decimal]:
    """Разобрать блок итогов раздела спеки в словарь «подпись → число».

    Вход: текст спеки и якорь раздела.
    Выход: словарь вида `{"Всего выплачено": Decimal("1066185.45"), ...}`.

    Подпись отделена от числа двумя и более пробелами; разряды внутри самого числа
    разделены одним пробелом, поэтому разбор идёт по ширине промежутка, а не по
    первому пробелу.
    """
    totals: dict[str, Decimal] = {}
    for block in _spec_code_blocks(_spec_section(spec_text, anchor)):
        for line in block.splitlines():
            parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
            if len(parts) != 2 or "=" in line:
                continue
            label, raw_value = parts
            try:
                totals[label] = to_decimal(raw_value)
            except Exception:  # строка блока не про число — пропускаем
                continue
    assert totals, f"в docs/spec.md после «{anchor}» не найдено блока итогов"
    return totals


def spec_named_values(spec_text: str, anchor: str) -> dict[str, Decimal]:
    """Разобрать блок промежуточных величин вида `имя = значение`.

    Вход: текст спеки и якорь раздела.
    Выход: словарь вида `{"i": Decimal("0.01"), "(1+i)^12": Decimal("1.1268…"), ...}`.

    Нужен разделам 3.1–3.2: спека приводит `i`, `(1+i)^n` и точное `A` явно,
    и тест сверяется именно с этими числами, а не с их пересказом.
    """
    values: dict[str, Decimal] = {}
    for block in _spec_code_blocks(_spec_section(spec_text, anchor)):
        for line in block.splitlines():
            if "=" not in line:
                continue
            name, _, raw_value = line.partition("=")
            try:
                values[name.strip()] = to_decimal(raw_value)
            except Exception:
                continue
    assert values, f"в docs/spec.md после «{anchor}» не найдено промежуточных величин"
    return values


# ------------------------------------------------------------------------ параметры


class Loan(NamedTuple):
    """Параметры кредита без досрочек и страховки."""

    principal: Decimal
    annual_rate: Decimal
    months: int


def loan(principal: str, annual_rate: str, months: int) -> Loan:
    """Собрать `Loan` из строк — деньги создаются только из строк (раздел 1 спеки)."""
    return Loan(Decimal(principal), Decimal(annual_rate), months)


#: Набор кредитов с фиксированным сроком, на которых проверяются формулы, инварианты
#: и правило 3.4. Ставки взяты и «круглые» (месячная ставка точна), и «неудобные»
#: (месячная ставка — бесконечная периодическая дробь), суммы — с некруглыми копейками:
#: хвостовая строка из находки Н-1 появляется именно на таких данных.
FIXED_TERM_LOANS = [
    loan("1000000.00", "0.12", 12),      # пример 6.1
    loan("753214.11", "0.095", 37),
    loan("99999.99", "0.137", 23),
    loan("12345.67", "0.185", 7),
    loan("250000.00", "0.073", 60),
    loan("1000000.01", "0.1201", 11),
    loan("77777.77", "0.0777", 13),
    loan("5000000.00", "0.2499", 84),
    loan("100.00", "0.001", 12),         # находка Н-2, раздел 4.1
    loan("120000.00", "0", 12),          # ставка 0 %, раздел 5
    loan("100.00", "0", 3),              # ставка 0 % с неделимым телом
    loan("100000.00", "0.12", 1),        # срок 1 месяц, раздел 5
    loan("1.00", "0.12", 1),
    loan("0.35", "0.12", 120),           # порог амортизации, раздел 5.1
]

LOAN_IDS = [f"{item.principal}-{item.annual_rate}-{item.months}м" for item in FIXED_TERM_LOANS]


# ------------------------------------------------------------------- инварианты (§4)
#
# Каждый инвариант — отдельная переиспользуемая проверка: раздел 4 спеки требует
# применять их ко всем эталонным примерам и к генерируемым кейсам, а не описывать
# заново в каждом тесте.


def check_i1_principal_sums_to_body(schedule: Sequence[ScheduleRow], principal: Decimal) -> None:
    """И-1: `Σ (D_k + E_k) = S`, точное равенство `Decimal`."""
    total = sum_money(row.principal + row.prepayment for row in schedule)
    assert_money_equal(total, principal, "И-1: сумма тел и досрочек")


def check_i2_closes_at_zero(schedule: Sequence[ScheduleRow]) -> None:
    """И-2: остаток после последнего платежа строго ноль, без допуска."""
    last = schedule[-1]
    assert last.balance == ZERO, (
        f"И-2: после последнего платежа (месяц {last.number}) остаток {last.balance}, "
        f"спека требует ровно 0.00"
    )


def check_i3_only_last_payment_deviates(
    schedule: Sequence[ScheduleRow], principal: Decimal, regular_payment: Decimal
) -> None:
    """И-3: последний платёж балансирующий и он единственный, отличный от `A`.

    Формула из раздела 3.4: `A_last = money(B_{last−1} + I_last)`. Спека подчёркивает,
    что отличаться от `A` вправе **только** последняя строка; допущение двух таких
    строк прячет дефект округления.
    """
    last = schedule[-1]
    previous_balance = schedule[-2].balance if len(schedule) > 1 else principal
    expected = spec_money(previous_balance + last.interest)
    assert_money_equal(last.payment, expected, "И-3: балансирующий последний платёж")

    deviating = [row.number for row in schedule if row.payment != regular_payment]
    assert deviating in ([], [last.number]), (
        f"И-3: от регулярного платежа {regular_payment} отличаются строки {deviating}, "
        f"спека разрешает отличаться только последней (месяц {last.number})"
    )


def check_i4_interest_does_not_increase(schedule: Sequence[ScheduleRow]) -> None:
    """И-4: `I_k ≥ I_{k+1}` — неравенство **нестрогое** (раздел 4.1, находка Н-2)."""
    for current, following in zip(schedule, schedule[1:]):
        assert current.interest >= following.interest, (
            f"И-4: проценты выросли с {current.interest} (месяц {current.number}) "
            f"до {following.interest} (месяц {following.number})"
        )


def check_i5_balance_strictly_decreases(
    schedule: Sequence[ScheduleRow], principal: Decimal
) -> None:
    """И-5: `B_k < B_{k−1}` для всех `k`, считая `B_0 = S`. Здесь строгое."""
    previous = principal
    for row in schedule:
        assert row.balance < previous, (
            f"И-5: остаток не убыл на месяце {row.number}: было {previous}, стало {row.balance}"
        )
        previous = row.balance


def check_i6_payment_covers_interest(schedule: Sequence[ScheduleRow]) -> None:
    """И-6: `A_k > I_k`, то есть `D_k > 0` — платёж всегда амортизирует долг."""
    for row in schedule:
        assert row.payment > row.interest, (
            f"И-6: месяц {row.number}: платёж {row.payment} не покрывает проценты {row.interest}"
        )
        assert row.principal > ZERO, (
            f"И-6: месяц {row.number}: тело платежа {row.principal}, долг не уменьшается"
        )


def check_i7_all_values_non_negative(schedule: Sequence[ScheduleRow]) -> None:
    """И-7: `A_k, I_k, D_k, E_k, B_k ≥ 0`."""
    for row in schedule:
        for name, value in (
            ("платёж", row.payment),
            ("проценты", row.interest),
            ("тело", row.principal),
            ("досрочка", row.prepayment),
            ("остаток", row.balance),
        ):
            assert value >= ZERO, f"И-7: месяц {row.number}: {name} отрицателен — {value}"


def check_i8_two_decimal_places(schedule: Sequence[ScheduleRow]) -> None:
    """И-8: каждая денежная величина графика записана ровно в 2 знака."""
    for row in schedule:
        assert isinstance(row.number, int), (
            f"номер месяца обязан быть int, получен {type(row.number).__name__}"
        )
        for name, value in (
            ("платёж", row.payment),
            ("проценты", row.interest),
            ("тело", row.principal),
            ("досрочка", row.prepayment),
            ("остаток", row.balance),
        ):
            assert isinstance(value, Decimal), (
                f"И-8: месяц {row.number}: {name} — {type(value).__name__}, а не Decimal"
            )
            assert -value.as_tuple().exponent == 2, (
                f"И-8: месяц {row.number}: {name} = {value} записано не в 2 знака"
            )


def check_i9_cash_flows_match(schedule: Sequence[ScheduleRow], principal: Decimal) -> None:
    """И-9: `Σ A_k + Σ E_k = S + Σ I_k` — деньги сходятся."""
    paid = sum_money(row.payment for row in schedule) + sum_money(
        row.prepayment for row in schedule
    )
    owed = principal + sum_money(row.interest for row in schedule)
    assert_money_equal(paid, owed, "И-9: выплачено против «тело + проценты»")


def check_i10_schedule_is_finite(schedule: Sequence[ScheduleRow], months: int) -> None:
    """И-10: число строк `≤ n`, нумерация сплошная с 1.

    Спека оговаривает, что строгое `< n` не гарантировано (раздел 4.2), поэтому здесь
    только верхняя граница.
    """
    assert schedule, "И-10: график пуст — раздел 5 запрещает пустой график как результат"
    assert len(schedule) <= months, (
        f"И-10: в графике {len(schedule)} строк при сроке {months} месяцев — "
        f"срок вырос, чего спека не допускает"
    )
    assert [row.number for row in schedule] == list(range(1, len(schedule) + 1)), (
        f"нумерация месяцев не сплошная: {[row.number for row in schedule]}"
    )


def check_all_invariants(schedule: Sequence[ScheduleRow], item: Loan) -> None:
    """Применить все инварианты раздела 4 к одному графику.

    Вход: построенный график и параметры кредита. Выход: `None`.
    Регулярный платёж для И-3 берётся формулой 3.2, воспроизведённой в тесте.
    """
    regular = spec_annuity_payment(item.principal, monthly_rate(item.annual_rate), item.months)
    check_i1_principal_sums_to_body(schedule, item.principal)
    check_i2_closes_at_zero(schedule)
    check_i3_only_last_payment_deviates(schedule, item.principal, regular)
    check_i4_interest_does_not_increase(schedule)
    check_i5_balance_strictly_decreases(schedule, item.principal)
    check_i6_payment_covers_interest(schedule)
    check_i7_all_values_non_negative(schedule)
    check_i8_two_decimal_places(schedule)
    check_i9_cash_flows_match(schedule, item.principal)
    check_i10_schedule_is_finite(schedule, item.months)


# ------------------------------------------------------- защита от зацикливания (§5.1)


def call_with_deadline(
    function: Callable, *args, seconds: float = NON_TERMINATION_DEADLINE_SECONDS, **kwargs
) -> tuple[bool, object, BaseException | None]:
    """Выполнить вызов в отдельном потоке и не ждать его дольше дедлайна.

    Вход: функция и её аргументы. Выход: `(завершился, результат, исключение)`.

    Нужен разделу 5.1: если защита по счётчику итераций в построителе отсутствует,
    вызов не вернётся никогда. Дедлайн не является ожиданием теста — он лишь не даёт
    подвесить весь прогон, а сам факт его срабатывания и есть дефект.
    """
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["result"] = function(*args, **kwargs)
        except BaseException as error:  # noqa: BLE001 — тест обязан увидеть любую ошибку
            outcome["error"] = error

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    return (not worker.is_alive()), outcome.get("result"), outcome.get("error")  # type: ignore[return-value]


def assert_does_not_amortise(item: Loan) -> None:
    """Проверить, что кредит отвергается ошибкой `PaymentDoesNotAmortise` и не виснет.

    Раздел 5.1 спеки: при `D_k ≤ 0` и `B_{k−1} > 0` построитель обязан поднять
    `PaymentDoesNotAmortise`, а не крутиться. Проверяется и тип ошибки, и то,
    что управление вернулось.
    """
    finished, result, error = call_with_deadline(
        build_schedule, item.principal, item.annual_rate, item.months
    )
    assert finished, (
        f"build_schedule({item.principal}, {item.annual_rate}, {item.months}) не вернул "
        f"управление за {NON_TERMINATION_DEADLINE_SECONDS} с: защиты по счётчику итераций "
        f"нет, график зациклился (раздел 5.1 спеки)"
    )
    assert error is not None, (
        f"ожидалась ошибка PaymentDoesNotAmortise, а график построился: {result}"
    )
    assert isinstance(error, PaymentDoesNotAmortise), (
        f"ожидалась PaymentDoesNotAmortise (раздел 5.1), получено "
        f"{type(error).__name__}: {error}"
    )


# ---------------------------------------------------------------------- фикстуры файла


@pytest.fixture(scope="session")
def ref_base_totals(spec_text: str) -> dict[str, Decimal]:
    """Итоги эталонного примера 6.1: выплачено, проценты, сумма тел, месяцев."""
    return spec_totals(spec_text, "### 6.1.")


@pytest.fixture(scope="session")
def ref_base_intermediates(spec_text: str) -> dict[str, Decimal]:
    """Промежуточные величины примера 6.1: `i`, `(1+i)^12`, точное и округлённое `A`."""
    return spec_named_values(spec_text, "### 6.1.")


# ============================================================ 3.1. Месячная ставка


def test_monthly_rate_of_reference_loan_is_exact(
    base_loan: dict, ref_base_intermediates: dict[str, Decimal]
) -> None:
    """Раздел 3.1: при `r = 12 %` месячная ставка равна ровно `0.01`.

    Значение берётся из блока промежуточных величин примера 6.1, а не из головы.
    """
    expected = ref_base_intermediates["i"]
    assert expected == Decimal("0.01"), (
        f"спека изменилась: раздел 6.1 приводит i = {expected}, тест ждал 0.01"
    )
    assert monthly_rate(base_loan["annual_rate"]) == expected, (
        f"monthly_rate(12 %) дал {monthly_rate(base_loan['annual_rate'])}, "
        f"раздел 3.1 требует {expected}"
    )


def test_monthly_rate_is_not_quantised_to_kopecks() -> None:
    """Раздел 3.1: `i` — точное частное `r / 12` и к 2 знакам **не приводится**.

    Спека приводит пример `r = 9.5 %` → `i = 0.0079166…`. Реализация, округлившая
    месячную ставку до копеек, дала бы `0.01` и разошлась бы с графиком на всей
    длине срока, а не в последней строке.
    """
    rate = Decimal("0.095")
    result = monthly_rate(rate)

    assert Decimal("0.0079166") <= result < Decimal("0.0079167"), (
        f"monthly_rate(9.5 %) дал {result}, раздел 3.1 требует 0.0079166…"
    )
    assert -result.as_tuple().exponent > 2, (
        f"monthly_rate(9.5 %) вернул {result} — ставка приведена к 2 знакам, "
        f"раздел 3.1 это запрещает"
    )
    assert abs(result * 12 - rate) <= Decimal("1e-20"), (
        f"monthly_rate(9.5 %) = {result}: обратное умножение на 12 даёт "
        f"{result * 12} вместо {rate}, частное посчитано слишком грубо для раздела 3.1"
    )


def test_monthly_rate_of_zero_rate_is_zero() -> None:
    """Раздел 3.1 и раздел 5: при `r = 0 %` месячная ставка — ноль, а не ошибка."""
    assert monthly_rate(Decimal("0")) == Decimal("0"), (
        f"monthly_rate(0 %) дал {monthly_rate(Decimal('0'))}, ожидается 0"
    )


# ========================================================= 3.2. Аннуитетный платёж


def test_reference_growth_factor_matches_spec(
    base_loan: dict, ref_base_intermediates: dict[str, Decimal]
) -> None:
    """Раздел 3.2: `(1 + i)^n` считается без округления.

    Спека приводит `(1+i)^12 = 1.126825030131969720661201` — 24 значащих цифры.
    Если бы `i` квантовалась или степень округлялась, это число не воспроизвелось бы.
    """
    expected = ref_base_intermediates["(1+i)^12"]
    with localcontext() as context:
        context.prec = SPEC_PRECISION
        growth = (Decimal(1) + monthly_rate(base_loan["annual_rate"])) ** base_loan["months"]

    assert growth == expected, (
        f"(1+i)^12 из месячной ставки реализации = {growth}, "
        f"раздел 6.1 спеки приводит {expected}"
    )


def test_reference_annuity_payment_matches_spec(
    base_loan: dict, ref_base_intermediates: dict[str, Decimal]
) -> None:
    """Раздел 3.2 и 6.1: `A = 88 848.79`, и это ровно округление точного значения.

    Проверяются обе величины, которые спека приводит явно: точное
    `88848.78867834170733998783122788652898045` и округлённое `88848.79`.
    Заодно фиксируется, что округление одно и в конце — расхождение с точным
    значением не превышает половины копейки.
    """
    exact = ref_base_intermediates["A (точный)"]
    rounded = ref_base_intermediates["A"]
    assert spec_money(exact) == rounded, (
        f"спека противоречива: округление точного {exact} даёт {spec_money(exact)}, "
        f"а раздел 6.1 приводит A = {rounded}"
    )

    result = annuity_payment(**base_loan)
    assert_money_equal(result, rounded, "A примера 6.1")
    assert abs(result - exact) <= KOPECK / 2, (
        f"A = {result} отличается от точного {exact} больше чем на полкопейки — "
        f"округление применено не один раз или не в конце (раздел 1.1)"
    )


@pytest.mark.parametrize("item", FIXED_TERM_LOANS, ids=LOAN_IDS)
def test_annuity_payment_follows_spec_formula(item: Loan) -> None:
    """Раздел 3.2: платёж совпадает с формулой спеки на всём наборе кредитов.

    Ожидание считается независимой реализацией формулы с запасом точности; обе
    ветки — `i > 0` и вырожденная `i = 0` — покрыты набором `FIXED_TERM_LOANS`.
    """
    expected = spec_annuity_payment(item.principal, monthly_rate(item.annual_rate), item.months)
    assert_money_equal(
        annuity_payment(item.principal, item.annual_rate, item.months),
        expected,
        f"A для {item.principal} / {item.annual_rate} / {item.months} мес",
    )


def test_annuity_payment_at_zero_rate_uses_degenerate_branch() -> None:
    """Раздел 3.2 и 5: при `i = 0` платёж равен `money(S / n)`, а не ошибке деления.

    Спека даёт контрольное значение: `120 000.00 / 0 % / 12 мес → A = 10 000.00`.
    Второй кейс — неделимое тело: `100.00 / 0 % / 3` даёт `33.33`, и остаток
    в копейку уходит в балансирующий платёж по правилу 3.4.
    """
    assert_money_equal(
        annuity_payment(Decimal("120000.00"), Decimal("0"), 12),
        Decimal("10000.00"),
        "A при ставке 0 %",
    )
    assert_money_equal(
        annuity_payment(Decimal("100.00"), Decimal("0"), 3),
        Decimal("33.33"),
        "A при ставке 0 % и неделимом теле",
    )


# ================================================== 6.1. Эталонный пример, построчно


def test_reference_schedule_matches_spec_row_by_row(
    base_loan: dict, ref_base_schedule: list[SpecRow]
) -> None:
    """Раздел 6.1: график сверяется с эталонной таблицей построчно, посимвольно.

    Сверяются все шесть полей строки (раздел 4.3), включая досрочку: в примере 6.1
    колонка опущена как пустая, но каждое `E_k` обязано быть `0.00`, а не `None`.
    """
    schedule = build_schedule(**base_loan)
    assert_schedule_equal(schedule, ref_base_schedule)


def test_reference_schedule_totals_match_spec(
    base_loan: dict, ref_base_totals: dict[str, Decimal]
) -> None:
    """Раздел 6.1: итоги — всего выплачено, проценты, сумма тел, число месяцев."""
    schedule = build_schedule(**base_loan)

    assert_money_equal(total_paid(schedule), ref_base_totals["Всего выплачено"], "всего выплачено")
    assert_money_equal(total_interest(schedule), ref_base_totals["Проценты"], "проценты за срок")
    assert_money_equal(
        sum_money(row.principal for row in schedule),
        ref_base_totals["Сумма тел"],
        "сумма тел",
    )
    assert len(schedule) == int(ref_base_totals["Месяцев"]), (
        f"месяцев в графике {len(schedule)}, спека требует {int(ref_base_totals['Месяцев'])}"
    )


def test_reference_last_payment_is_balancing(
    base_loan: dict, ref_base_schedule: list[SpecRow]
) -> None:
    """Раздел 3.4: последний платёж примера 6.1 меньше регулярного на 3 копейки.

    Это и есть балансировка: `A_12 = money(B_11 + I_12)`. Проверяется не «меньше»,
    а точное равенство формуле — в примере 6.2b знак балансировки противоположный,
    и ожидание «последний платёж всегда меньше» там ложно упало бы.
    """
    schedule = build_schedule(**base_loan)
    last = schedule[-1]
    previous = schedule[-2]
    regular = annuity_payment(**base_loan)

    assert_money_equal(
        last.payment, spec_money(previous.balance + last.interest), "балансирующий платёж"
    )
    assert last.payment == regular - Decimal("0.03"), (
        f"последний платёж {last.payment}, спека (раздел 6.1) требует {regular} − 0.03"
    )
    deviating = [row.number for row in schedule if row.payment != regular]
    assert deviating == [last.number], (
        f"от регулярного платежа отличаются строки {deviating}, "
        f"спека (И-3) допускает только последнюю"
    )


def test_reference_example_does_not_cover_condition_b(base_loan: dict) -> None:
    """Разделы 3.4 и 7 (Н-1): пример 6.1 сам по себе условие `k = n` не проверяет.

    На 12-м месяце примера 6.1 срабатывает условие (а): `A − I_12 ≥ B_11`
    (`87 969.10 ≥ 87 969.07`). Значит реализация без условия (б) прошла бы этот
    эталон и упала бы только на данных находки Н-1. Тест фиксирует это явно,
    чтобы прохождение примера 6.1 не считалось покрытием правила 3.4 целиком.
    """
    schedule = build_schedule(**base_loan)
    regular = annuity_payment(**base_loan)
    last = schedule[-1]
    previous_balance = schedule[-2].balance

    assert regular - last.interest >= previous_balance, (
        f"условие 3.4(а) на последнем месяце примера 6.1 не выполнено: "
        f"{regular} − {last.interest} < {previous_balance}. Тогда эталон 6.1 стал бы "
        f"кейсом на условие (б), и находка Н-1 требует пересмотра"
    )


# ============================== 3.4(б). Хвостовая строка при фиксированном сроке (Н-1)


@pytest.mark.parametrize("item", FIXED_TERM_LOANS, ids=LOAN_IDS)
def test_fixed_term_schedule_has_no_kopeck_tail(item: Loan) -> None:
    """Раздел 3.4(б), находка Н-1: при фиксированном сроке хвоста в копейку нет.

    Если реализация закрывает график только по условию (а), накопленное округление
    оставляет остаток `0.01` и появляется лишняя строка сверх срока — ровно то,
    что спека описывает для примера 2b. Здесь проверяется общее следствие:
    строк не больше `n`, последняя закрывает долг в ноль и равна `money(B + I)`.
    """
    schedule = build_schedule(item.principal, item.annual_rate, item.months)
    last = schedule[-1]
    previous_balance = schedule[-2].balance if len(schedule) > 1 else item.principal

    assert len(schedule) <= item.months, (
        f"строк {len(schedule)} при сроке {item.months}: похоже на хвостовую строку "
        f"из находки Н-1 — платёж {last.payment}, остаток предыдущей строки {previous_balance}"
    )
    assert last.balance == ZERO, (
        f"график не закрылся: после месяца {last.number} остаток {last.balance}"
    )
    assert_money_equal(
        last.payment,
        spec_money(previous_balance + last.interest),
        f"последний платёж ({item.principal} / {item.annual_rate} / {item.months} мес)",
    )


@pytest.mark.parametrize("item", FIXED_TERM_LOANS, ids=LOAN_IDS)
def test_payment_decomposition_follows_spec(item: Loan) -> None:
    """Раздел 3.3 и 3.4: разложение каждого платежа на проценты, тело и остаток.

    `I_k = money(B_{k−1} · i)`, `D_k = A_k − I_k`, `B_k = B_{k−1} − D_k − E_k`.
    В последней строке дополнительно действует правило 3.4: платёж балансирующий,
    тело равно всему остатку, остаток обнуляется.
    """
    rate = monthly_rate(item.annual_rate)
    schedule = build_schedule(item.principal, item.annual_rate, item.months)
    regular = annuity_payment(item.principal, item.annual_rate, item.months)

    previous_balance = item.principal
    for row in schedule:
        with localcontext() as context:
            context.prec = SPEC_PRECISION
            expected_interest = spec_money(previous_balance * rate)
        assert_money_equal(row.interest, expected_interest, f"месяц {row.number}, проценты")
        assert_money_equal(
            row.principal, row.payment - row.interest, f"месяц {row.number}, тело"
        )
        assert_money_equal(
            row.balance,
            previous_balance - row.principal - row.prepayment,
            f"месяц {row.number}, остаток",
        )
        if row is not schedule[-1]:
            assert_money_equal(row.payment, regular, f"месяц {row.number}, регулярный платёж")
        previous_balance = row.balance

    last = schedule[-1]
    balance_before_last = schedule[-2].balance if len(schedule) > 1 else item.principal
    assert_money_equal(last.principal, balance_before_last, "тело последнего платежа")


# ============================================================= 4. Инварианты И-1…И-10


@pytest.mark.parametrize("item", FIXED_TERM_LOANS, ids=LOAN_IDS)
def test_all_invariants_hold(item: Loan) -> None:
    """Раздел 4: все инварианты И-1…И-10 на каждом кредите набора.

    Набор намеренно включает вырожденные кейсы — ставку 0 %, срок 1 месяц,
    кредит `100.00 / 0.1 %` из находки Н-2 и порог амортизации `0.35`: раздел 4
    требует выполнения инвариантов «всегда, в любом сценарии, включая вырожденные».
    """
    schedule = build_schedule(item.principal, item.annual_rate, item.months)
    check_all_invariants(schedule, item)


def test_invariants_hold_on_reference_schedule(base_loan: dict) -> None:
    """Раздел 4: инварианты на эталонном примере 6.1 — отдельно от набора.

    Пример 6.1 — единственный график, сверенный со спекой построчно, поэтому
    инварианты применяются к нему явным тестом, а не только в параметризации.
    """
    schedule = build_schedule(**base_loan)
    check_all_invariants(
        schedule,
        Loan(base_loan["principal"], base_loan["annual_rate"], base_loan["months"]),
    )


def test_row_has_six_fields_of_spec() -> None:
    """Раздел 4.3: строка графика содержит шесть полей в порядке спеки.

    Без раздельных «тела» и «досрочки» инвариант И-1 (`Σ (D_k + E_k) = S`)
    не проверить, а фактически применённую досрочку нечем вернуть вызывающему.
    """
    assert ScheduleRow._fields == (
        "number",
        "payment",
        "interest",
        "principal",
        "prepayment",
        "balance",
    ), f"поля строки графика: {ScheduleRow._fields}, раздел 4.3 требует шесть в ином составе"


# ================================ 4.1. Находка Н-2 — совпадение соседних процентов


def test_equal_interest_schedule_matches_spec(
    spec_text: str, ref_equal_interest_schedule: list[SpecRow]
) -> None:
    """Раздел 4.1 (Н-2): кредит `100.00 / 0.1 % / 12 мес` сверяется с таблицей спеки.

    Таблица раздела 4.1 сокращена строкой `…`, поэтому месяцев 7–11 в ней нет:
    сверяются приведённые месяцы по их номерам, а длина графика — по тексту
    раздела («закрывается за 12 месяцев с остатком 0.00»).
    """
    schedule = build_schedule(Decimal("100.00"), Decimal("0.001"), 12)
    expected_payment = to_decimal(re.search(r"`A = ([\d.]+)`", spec_text).group(1))

    assert_money_equal(
        annuity_payment(Decimal("100.00"), Decimal("0.001"), 12), expected_payment, "A находки Н-2"
    )
    assert len(schedule) == 12, (
        f"график Н-2 занял {len(schedule)} месяцев, раздел 4.1 требует 12"
    )

    actual_by_month = {row.number: row for row in schedule}
    for expected in ref_equal_interest_schedule:
        assert expected.number in actual_by_month, (
            f"в графике нет месяца {expected.number}, приведённого в таблице раздела 4.1"
        )
        row = actual_by_month[expected.number]
        assert_money_equal(row.payment, expected.payment, f"Н-2, месяц {expected.number}, платёж")
        assert_money_equal(
            row.interest, expected.interest, f"Н-2, месяц {expected.number}, проценты"
        )
        assert_money_equal(row.principal, expected.principal, f"Н-2, месяц {expected.number}, тело")
        assert_money_equal(
            row.prepayment, expected.prepayment, f"Н-2, месяц {expected.number}, досрочка"
        )
        assert_money_equal(row.balance, expected.balance, f"Н-2, месяц {expected.number}, остаток")


def test_adjacent_interest_may_be_equal(spec_text: str) -> None:
    """Раздел 4.1 (Н-2): соседние ненулевые проценты совпадают — И-4 нестрогое.

    Спека: «Строгое убывание — False, неубывание — True». Проверяется именно это:
    неубывание держится, строгое убывание нарушается, а копеечные проценты стоят
    пять месяцев подряд. Тест, написанный на строгое `I_k > I_{k+1}`, упал бы здесь
    ложно — ради этого кейс и внесён в регресс.
    """
    schedule = build_schedule(Decimal("100.00"), Decimal("0.001"), 12)
    interests = [row.interest for row in schedule]

    check_i4_interest_does_not_increase(schedule)

    equal_non_zero = [
        (first.number, second.number)
        for first, second in zip(schedule, schedule[1:])
        if first.interest == second.interest and first.interest > ZERO
    ]
    assert equal_non_zero, (
        f"проценты по месяцам {interests}: пары равных ненулевых процентов не найдено, "
        f"хотя раздел 4.1 приводит пять таких месяцев подряд"
    )
    assert not all(
        first > second for first, second in zip(interests, interests[1:])
    ), "проценты убывают строго — данные разошлись с находкой Н-2 раздела 4.1"

    kopeck_months = [row.number for row in schedule if row.interest == KOPECK]
    assert kopeck_months == [1, 2, 3, 4, 5], (
        f"проценты в копейку держатся месяцы {kopeck_months}, "
        f"раздел 4.1 приводит ровно месяцы 1–5"
    )

    expected_total = to_decimal(
        re.search(r"Проценты за весь срок: `([\d.]+)`", spec_text).group(1)
    )
    assert total_interest(schedule) == expected_total, (
        f"проценты за срок {total_interest(schedule)}, раздел 4.1 приводит {expected_total}"
    )


# ================================================== 5. Вырожденные случаи и границы


def test_zero_rate_schedule() -> None:
    """Раздел 5, строка «Ставка 0 %»: допустима, `A = money(S / n)`, все `I_k = 0`.

    Контрольные значения спеки: `120 000.00 / 0 % / 12 мес` → `A = 10 000.00`,
    12 строк, проценты `0.00`, остаток `0.00`.
    """
    item = loan("120000.00", "0", 12)
    schedule = build_schedule(item.principal, item.annual_rate, item.months)

    assert len(schedule) == 12, f"строк {len(schedule)}, спека требует 12"
    for row in schedule:
        assert_money_equal(row.interest, ZERO, f"месяц {row.number}, проценты при ставке 0 %")
        assert_money_equal(row.payment, Decimal("10000.00"), f"месяц {row.number}, платёж")
    assert_money_equal(schedule[-1].balance, ZERO, "остаток после последнего платежа")
    assert_money_equal(total_interest(schedule), ZERO, "проценты за срок при ставке 0 %")
    assert_money_equal(total_paid(schedule), item.principal, "всего выплачено при ставке 0 %")
    check_all_invariants(schedule, item)


def test_zero_rate_balances_indivisible_body() -> None:
    """Разделы 3.2, 3.4 и 5: при ставке 0 % балансировка тоже обязана работать.

    `100.00 / 0 % / 3` — тело не делится на срок нацело: `A = 33.33`, и последний
    платёж обязан забрать оставшуюся копейку, иначе И-1 и И-2 не сойдутся.
    """
    item = loan("100.00", "0", 3)
    schedule = build_schedule(item.principal, item.annual_rate, item.months)

    assert len(schedule) == 3, f"строк {len(schedule)}, ожидается 3"
    assert_money_equal(schedule[0].payment, Decimal("33.33"), "месяц 1, платёж")
    assert_money_equal(schedule[1].payment, Decimal("33.33"), "месяц 2, платёж")
    assert_money_equal(schedule[2].payment, Decimal("33.34"), "месяц 3, балансирующий платёж")
    assert_money_equal(schedule[-1].balance, ZERO, "остаток")
    check_all_invariants(schedule, item)


def test_single_month_schedule() -> None:
    """Раздел 5, строка «Срок 1 месяц»: единственный платёж — он же балансирующий.

    Контрольные значения спеки: `100 000.00 / 12 % / 1 мес` → `A_1 = 101 000.00`,
    `I_1 = 1 000.00`, `D_1 = 100 000.00`, `B_1 = 0`.
    """
    item = loan("100000.00", "0.12", 1)
    schedule = build_schedule(item.principal, item.annual_rate, item.months)

    assert len(schedule) == 1, f"строк {len(schedule)}, спека требует одну"
    row = schedule[0]
    assert row.number == 1, f"номер единственного месяца — {row.number}, ожидается 1"
    assert_money_equal(row.payment, Decimal("101000.00"), "A_1")
    assert_money_equal(row.interest, Decimal("1000.00"), "I_1")
    assert_money_equal(row.principal, Decimal("100000.00"), "D_1")
    assert_money_equal(row.prepayment, ZERO, "E_1")
    assert_money_equal(row.balance, ZERO, "B_1")
    assert_money_equal(
        annuity_payment(item.principal, item.annual_rate, item.months),
        Decimal("101000.00"),
        "A при сроке 1 месяц",
    )
    check_all_invariants(schedule, item)


@pytest.mark.parametrize(
    "item, expected_payment, expected_interest",
    [
        (loan("0.01", "0", 1), Decimal("0.01"), ZERO),
        (loan("0.01", "0.12", 1), Decimal("0.01"), ZERO),
        (loan("1.00", "0.12", 1), Decimal("1.01"), KOPECK),
        (loan("1.00", "0", 1), Decimal("1.00"), ZERO),
    ],
    ids=["копейка-0%", "копейка-12%", "рубль-12%", "рубль-0%"],
)
def test_kopeck_scale_loans(item: Loan, expected_payment: Decimal, expected_interest: Decimal) -> None:
    """Границы в деньгах: кредит размером в копейку и в рубль на один месяц.

    Раздел 5 («Срок 1 месяц») в пределе: `A_1 = money(S + money(S·i))`, `D_1 = S`,
    `B_1 = 0`. Проценты на копейку меньше половины копейки и округляются в ноль —
    И-6 при этом обязан держаться за счёт тела платежа.
    """
    schedule = build_schedule(item.principal, item.annual_rate, item.months)

    assert len(schedule) == 1, f"строк {len(schedule)}, ожидается одна"
    row = schedule[0]
    assert_money_equal(row.payment, expected_payment, "платёж")
    assert_money_equal(row.interest, expected_interest, "проценты")
    assert_money_equal(row.principal, item.principal, "тело")
    assert_money_equal(row.balance, ZERO, "остаток")
    check_all_invariants(schedule, item)


# ============================== 5.1. Платёж не покрывает проценты, порог амортизации


def test_amortisation_threshold_is_thirty_five_kopecks() -> None:
    """Раздел 5.1: `0.35` — наименьшее тело, которое ещё амортизируется.

    Параметры порога заданы спекой жёстко: ставка 12 %, `n = 120`. Контрольные
    величины первой строки — `A = 0.01`, `I_1 = 0.00`, `D_1 = 0.01`.
    """
    item = loan("0.35", "0.12", 120)
    schedule = build_schedule(item.principal, item.annual_rate, item.months)
    first = schedule[0]

    assert_money_equal(
        annuity_payment(item.principal, item.annual_rate, item.months), KOPECK, "A на пороге 0.35"
    )
    assert_money_equal(first.payment, KOPECK, "порог 0.35: A_1")
    assert_money_equal(first.interest, ZERO, "порог 0.35: I_1")
    assert_money_equal(first.principal, KOPECK, "порог 0.35: D_1")
    assert_money_equal(schedule[-1].balance, ZERO, "порог 0.35: остаток в конце")
    check_all_invariants(schedule, item)


def test_below_threshold_does_not_amortise() -> None:
    """Раздел 5.1: тело `0.34` при тех же параметрах уже не амортизируется.

    Порог `0.35` назван спекой наименьшим, значит соседняя копейка снизу обязана
    отвергаться ошибкой `PaymentDoesNotAmortise`, а не строить бесконечный график.
    """
    assert_does_not_amortise(loan("0.34", "0.12", 120))


def test_non_amortising_loan_is_rejected_not_looped() -> None:
    """Раздел 5.1: воспроизведение `S = 1.00`, ставка 12 %, `n = 120`.

    Спека приводит расчёт: `A = 0.01`, `I_1 = 0.01`, `D_1 = 0.00` — остаток
    не сдвигается. Построитель обязан поднять `PaymentDoesNotAmortise` по счётчику
    итераций; тест выполняется с дедлайном, чтобы отсутствие защиты проявилось
    сообщением, а не зависанием прогона.
    """
    item = loan("1.00", "0.12", 120)
    assert_money_equal(
        annuity_payment(item.principal, item.annual_rate, item.months),
        KOPECK,
        "A из воспроизведения 5.1",
    )
    assert_does_not_amortise(item)


def test_zero_rate_payment_rounding_to_zero_is_rejected() -> None:
    """Раздел 5.1 в применении к ставке 0 %: `A = money(0.01 / 12) = 0.00`.

    Вырожденная ветка 3.2 тоже способна дать нулевой платёж. Тогда `D_k = 0`
    при `B_{k−1} > 0`, что нарушает И-6 и подпадает под требование раздела 5.1:
    поднимать `PaymentDoesNotAmortise`, а не крутиться. Спека этот стык прямо
    не оговаривает — ожидание выведено из И-6 и правила 5.1.
    """
    assert_does_not_amortise(loan("0.01", "0", 12))


def test_error_types_follow_contract() -> None:
    """Разделы 5 и 5.1: типы ошибок, на которые вправе рассчитывать вызывающий код.

    Валидация входа — `ValueError`; неамортизируемость — `PaymentDoesNotAmortise`,
    её частный случай; незавершившийся график — `RuntimeError`, а не тихий возврат.
    """
    assert issubclass(PaymentDoesNotAmortise, ValueError), (
        "PaymentDoesNotAmortise обязана быть ValueError: раздел 5 обещает вызывающему "
        "именно ошибку значения"
    )
    assert issubclass(ScheduleDidNotTerminate, RuntimeError), (
        "ScheduleDidNotTerminate обязана быть RuntimeError: это отказ защиты, "
        "а не некорректный вход"
    )


# ============================================================= 5. Валидация входа


INVALID_LOANS = [
    (loan("-1.00", "0.12", 12), "отрицательная сумма"),
    (loan("-0.01", "0.12", 12), "сумма в минус копейку"),
    (loan("0.00", "0.12", 12), "нулевая сумма"),
    (loan("1000000.00", "-0.01", 12), "отрицательная ставка"),
    (loan("1000000.00", "-0.12", 12), "отрицательная ставка, 12 %"),
    (loan("1000000.00", "0.12", 0), "нулевой срок"),
    (loan("1000000.00", "0.12", -1), "отрицательный срок"),
]

INVALID_IDS = [reason for _, reason in INVALID_LOANS]


@pytest.mark.parametrize("item, reason", INVALID_LOANS, ids=INVALID_IDS)
def test_invalid_loan_is_rejected(item: Loan, reason: str) -> None:
    """Раздел 5: недопустимый вход отвергается `ValueError` до всякого расчёта.

    Предусловия раздела 2 и 5: `S_req > 0`, `r ≥ 0`, `n` — целое `≥ 1`. Пустой
    график недопустим как результат: он маскирует дефект вызывающего кода.
    Проверяются все три входные точки — валидатор, формула платежа и построитель.
    """
    entry_points = {
        "validate_loan": lambda: validate_loan(item.principal, item.annual_rate, item.months),
        "annuity_payment": lambda: annuity_payment(item.principal, item.annual_rate, item.months),
        "build_schedule": lambda: build_schedule(item.principal, item.annual_rate, item.months),
    }
    for name, call in entry_points.items():
        with pytest.raises(ValueError) as raised:
            call()
        assert not isinstance(raised.value, PaymentDoesNotAmortise), (
            f"{name}({reason}) отверг вход как неамортизируемый ({raised.value}), "
            f"а раздел 5 требует ошибки валидации: расчёт не должен был начаться"
        )


@pytest.mark.parametrize(
    "item",
    [
        loan("1000000.00", "0.12", 12),
        loan("0.01", "0", 1),
        loan("100.00", "0.001", 12),
    ],
    ids=["пример-6.1", "копейка-0%", "находка-Н-2"],
)
def test_valid_loan_passes_validation(item: Loan) -> None:
    """Раздел 5: допустимый вход валидатор пропускает молча.

    Граничные значения предусловий — сумма в копейку, ставка ровно `0 %`,
    срок ровно 1 месяц — находятся внутри области, а не вне её.
    """
    assert validate_loan(item.principal, item.annual_rate, item.months) is None, (
        f"validate_loan({item.principal}, {item.annual_rate}, {item.months}) вернул значение; "
        f"валидатор обязан молча пропускать корректный вход"
    )


# ================================================================ 1. Запрет float


FLOAT_CALLS = [
    ("monthly_rate(ставка float)", lambda: monthly_rate(0.12)),
    ("annuity_payment(сумма float)", lambda: annuity_payment(1000000.0, Decimal("0.12"), 12)),
    ("annuity_payment(ставка float)", lambda: annuity_payment(Decimal("1000000.00"), 0.12, 12)),
    ("build_schedule(сумма float)", lambda: build_schedule(1000000.0, Decimal("0.12"), 12)),
    ("build_schedule(ставка float)", lambda: build_schedule(Decimal("1000000.00"), 0.12, 12)),
    ("validate_loan(сумма float)", lambda: validate_loan(1000000.0, Decimal("0.12"), 12)),
    ("validate_loan(ставка float)", lambda: validate_loan(Decimal("1000000.00"), 0.12, 12)),
]


@pytest.mark.parametrize("label, call", FLOAT_CALLS, ids=[label for label, _ in FLOAT_CALLS])
def test_float_arguments_are_rejected(label: str, call: Callable) -> None:
    """Раздел 1: `float` в денежных аргументах — `TypeError`, а не молчаливый расчёт.

    `Decimal(0.1)` даёт `0.1000000000000000055511151231257827…`; такой дефект
    не виден до сверки копейки на последнем платеже, поэтому ошибка обязана
    возникать в точке передачи аргумента.
    """
    with pytest.raises(TypeError):
        call()


def test_reject_float_helper_contract() -> None:
    """Раздел 1: `reject_float` пропускает `Decimal` и отвергает `float`.

    Это точка, через которую запрет float входит во все публичные функции;
    её поведение проверяется отдельно от вызывающих.
    """
    assert reject_float(Decimal("0.1"), "ставка") is None, (
        "reject_float обязан молча пропускать Decimal"
    )
    with pytest.raises(TypeError):
        reject_float(0.1, "ставка")


@pytest.mark.parametrize(
    "months", [12.0, 0.5], ids=["срок-12.0", "срок-0.5"]
)
def test_float_term_is_rejected(months: float) -> None:
    """Раздел 2: `n` — целое число месяцев, дробный срок недопустим.

    Спека фиксирует тип `n` как `int`, но не называет класс ошибки, поэтому
    принимается любая из двух: `TypeError` (не тот тип) или `ValueError`
    (значение вне области). Молчаливый расчёт по дробному сроку — дефект:
    `12.0` протащит float в денежную арифметику.
    """
    with pytest.raises((TypeError, ValueError)):
        build_schedule(Decimal("1000000.00"), Decimal("0.12"), months)
