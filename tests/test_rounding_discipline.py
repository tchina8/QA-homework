"""Тест-страж дисциплины округления и запрета float.

Раздел 1.1 спеки («Контракт округления»): во всём пакете `calc/` округление денег
выполняется единственной функцией `money` из `calc/money.py`. Ни один другой модуль
не приводит числа к копейкам самостоятельно и не создаёт `Decimal` из `float`.

Это статическая проверка исходников: она читает файлы как текст и разбирает их AST,
а не вызывает функции. Такой тест ловит то, что не поймает ни один расчётный кейс, —
второе место округления, добавленное «на всякий случай», которое разойдётся
с первым на границе половины копейки.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import REPO_ROOT

CALC_DIR = REPO_ROOT / "calc"

#: Единственный модуль, которому спека разрешает округлять деньги.
ROUNDING_MODULE = "money.py"

#: Токены округления, запрещённые вне `money.py`.
ROUNDING_TOKENS = ("quantize", "ROUND_HALF_UP", "ROUND_CEILING", "ROUND_FLOOR", "ROUND_DOWN")


def calc_modules() -> list[Path]:
    """Все модули пакета `calc`.

    Вход: нет. Выход: отсортированный список путей к `.py`.
    """
    modules = sorted(CALC_DIR.glob("*.py"))
    assert modules, f"в {CALC_DIR} не найдено ни одного модуля — проверять нечего"
    return modules


@pytest.mark.parametrize("module", calc_modules(), ids=lambda p: p.name)
def test_rounding_lives_only_in_money_module(module: Path) -> None:
    """Округление встречается только в `calc/money.py`.

    Раздел 1.1 спеки. Любое упоминание округляющих токенов в другом модуле пакета —
    нарушение контракта единственной точки округления.
    """
    source = module.read_text(encoding="utf-8")
    found = [token for token in ROUNDING_TOKENS if token in source]
    if module.name == ROUNDING_MODULE:
        assert "quantize" in source, (
            "calc/money.py обязан содержать саму операцию округления — "
            "раздел 1.1 спеки называет его единственной точкой округления"
        )
        return
    assert not found, (
        f"{module.name} округляет самостоятельно: найдены {found}. "
        f"Раздел 1.1 спеки разрешает это только в {ROUNDING_MODULE}"
    )


def test_money_module_rounds_in_exactly_one_place() -> None:
    """В `calc/money.py` ровно один вызов округления.

    Раздел 1.1 спеки говорит про «единственную функцию». Две операции округления
    внутри самого модуля — то же нарушение, просто спрятанное на уровень глубже.
    """
    source = (CALC_DIR / ROUNDING_MODULE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "quantize"
    ]
    assert len(calls) == 1, (
        f"в calc/{ROUNDING_MODULE} найдено вызовов округления: {len(calls)} "
        f"(строки {[c.lineno for c in calls]}), спека требует ровно один"
    )


@pytest.mark.parametrize("module", calc_modules(), ids=lambda p: p.name)
def test_no_float_literals_in_calc(module: Path) -> None:
    """Ни один модуль `calc` не содержит литералов `float`.

    Раздел 1 спеки: деньги — `Decimal`, создаваемый только из строк, никогда
    из `float`. Литерал `0.12` в исходнике — уже потерянная точность, независимо
    от того, во что его потом обернут.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    offenders = [
        f"строка {node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert not offenders, (
        f"{module.name} содержит литералы float — {offenders}. "
        f"Раздел 1 спеки запрещает float в денежных расчётах"
    )


@pytest.mark.parametrize("module", calc_modules(), ids=lambda p: p.name)
def test_no_decimal_built_from_float(module: Path) -> None:
    """Ни один модуль `calc` не строит `Decimal` из `float`.

    Раздел 1 спеки. `Decimal(0.1)` даёт `0.1000000000000000055511151231257827…`,
    и такой дефект не виден до сверки копейки на последнем платеже.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "Decimal":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, float):
                offenders.append(f"строка {node.lineno}: Decimal({argument.value!r})")
    assert not offenders, (
        f"{module.name} создаёт Decimal из float — {offenders}. Раздел 1 спеки: "
        f"деньги создаются только из строк"
    )


def test_money_rounds_half_up_at_the_boundary() -> None:
    """`money` округляет по правилу `ROUND_HALF_UP`, а не банковским округлением.

    Раздел 1 спеки прямо называет режим. Проверяется именно граница половины копейки:
    режим по умолчанию в `decimal` — `ROUND_HALF_EVEN`, и на `0.005` он даёт `0.00`
    вместо требуемого `0.01`. Отличаются эти режимы только здесь, поэтому кейс
    с обычными суммами дефект не поймает.
    """
    from calc.money import money

    assert money(Decimal("0.005")) == Decimal("0.01"), (
        "money(0.005) обязан дать 0.01: раздел 1 спеки требует ROUND_HALF_UP, "
        "а ROUND_HALF_EVEN дал бы 0.00"
    )
    assert money(Decimal("0.015")) == Decimal("0.02"), (
        "money(0.015) обязан дать 0.02: ROUND_HALF_EVEN дал бы те же 0.02, "
        "а вот на 0.025 режимы расходятся — см. следующую проверку"
    )
    assert money(Decimal("0.025")) == Decimal("0.03"), (
        "money(0.025) обязан дать 0.03: здесь ROUND_HALF_EVEN дал бы 0.02"
    )
    assert money(Decimal("-0.005")) == Decimal("-0.01"), (
        "ROUND_HALF_UP округляет от нуля в обе стороны"
    )


def test_money_rejects_float() -> None:
    """`money` отвергает `float` явной ошибкой, а не молча теряет точность.

    Раздел 1 спеки: деньги никогда не создаются из `float`. Ошибка обязана
    возникать в точке передачи аргумента.
    """
    from calc.money import money

    with pytest.raises(TypeError):
        money(0.1)
