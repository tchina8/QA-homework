"""Детерминированные шаги регрессионного прогона кредитного калькулятора.

Каждая подкоманда — один шаг workflow. Каждый шаг оставляет файл-артефакт
в `reports/steps/`, по которому видно, что он выполнен и с каким результатом.
Ни один шаг не выносит суждений: суждение — работа проверяющего субагента
(шаг 6, см. `workflow/regression-run.md`).

Запуск (обязательно из корня репозитория, интерпретатор — только из .venv):

    .venv\\Scripts\\python.exe workflow\\run_regression.py step1
    .venv\\Scripts\\python.exe workflow\\run_regression.py step2 --stamp 2026-08-27_181500
    ...
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS = REPO_ROOT / "reports"
RAW_DIR = REPORTS / "raw"
STEPS_DIR = REPORTS / "steps"
PYTEST = REPO_ROOT / ".venv" / "Scripts" / "pytest.exe"

#: Статусы pytest, которые разбирает шаг 2. XPASS означает, что xfail-тест
#: неожиданно прошёл — то есть баг починили, не сняв пометку.
STATUSES = ("PASSED", "FAILED", "ERROR", "XFAIL", "XPASS", "SKIPPED")

#: Node id параметризованного теста **содержит пробелы** — метки вида
#: `[отрицательная ставка, 12 %]`. Поэтому `.+?`, а не `\S+`: с `\S+` разбор молча
#: терял ровно такие строки, и отчёт занижал число тестов, не подавая никакого
#: сигнала. Дефект найден проверяющим агентом на первом же прогоне workflow.
LINE = re.compile(
    r"^(?P<file>tests/[\w\-.]+\.py)::(?P<test>.+?)\s+(?P<status>" + "|".join(STATUSES) + r")\b"
)

#: Строка pytest «collected N items» — контрольная сумма разбора.
COLLECTED = re.compile(r"collected\s+(?P<count>\d+)\s+item")

#: Итоговая строка pytest вида «265 passed, 1 xfailed in 0.42s».
SUMMARY_ITEM = re.compile(r"(?P<count>\d+)\s+(?P<status>passed|failed|xfailed|xpassed|skipped|error)")


def stamp_now() -> str:
    """Отметка прогона: дата и время.

    Вход: нет. Выход: строка `ГГГГ-ММ-ДД_ЧЧММСС`.
    Время в имени нужно, чтобы за один день помещалось несколько прогонов
    и шаг 3 мог найти именно предыдущий, а не затёртый.
    """
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def write_json(path: Path, payload: dict) -> Path:
    """Записать артефакт шага.

    Вход: путь и словарь. Выход: тот же путь.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_json(path: Path) -> dict:
    """Прочитать артефакт предыдущего шага, внятно упав, если его нет."""
    if not path.exists():
        raise SystemExit(
            f"нет артефакта {path.relative_to(REPO_ROOT)} — предыдущий шаг не выполнен"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ шаг 1: прогон


def step1(stamp: str) -> None:
    """Прогнать тесты и сохранить сырой вывод.

    Артефакты: `reports/raw/<stamp>.txt` и `reports/steps/<stamp>-01-run.json`.
    Код возврата pytest не глушится: если прогон упал, это должно быть видно.
    """
    if not PYTEST.exists():
        raise SystemExit(f"не найден {PYTEST} — пересоздайте .venv по инструкции в CLAUDE.md")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"{stamp}.txt"

    started = datetime.now()
    completed = subprocess.run(
        [str(PYTEST), "-v", "--tb=long", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    finished = datetime.now()
    raw_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")

    write_json(
        STEPS_DIR / f"{stamp}-01-run.json",
        {
            "step": 1,
            "name": "прогон",
            "stamp": stamp,
            "command": f"{PYTEST.name} -v --tb=long",
            "exit_code": completed.returncode,
            "started": started.isoformat(timespec="seconds"),
            "finished": finished.isoformat(timespec="seconds"),
            "duration_seconds": round((finished - started).total_seconds(), 2),
            "raw_output": str(raw_path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "raw_bytes": raw_path.stat().st_size,
        },
    )
    print(f"шаг 1: {raw_path.relative_to(REPO_ROOT)}, код возврата {completed.returncode}")


def _check_consistency(raw_text: str, tests: dict[str, str], totals: dict[str, int]) -> dict:
    """Сверить разбор с тем, что pytest сказал о себе сам.

    Вход: сырой вывод, разобранные тесты и итоги.
    Выход: словарь с полем `ok` и списком расхождений.

    Две независимые контрольные суммы: строка `collected N items` и итоговая строка
    прогона. Без них дефект разбора проходит насквозь в отчёт и делает прогон
    зеленее, чем он есть, — ровно это и случилось на первом прогоне workflow.
    """
    problems: list[str] = []

    collected_match = COLLECTED.search(raw_text)
    collected = int(collected_match["count"]) if collected_match else None
    if collected is not None and collected != len(tests):
        problems.append(
            f"pytest собрал {collected} тестов, разобрано {len(tests)} "
            f"(потеряно {collected - len(tests)})"
        )

    tail = raw_text.strip().splitlines()[-1] if raw_text.strip() else ""
    summary = {
        match["status"]: int(match["count"]) for match in SUMMARY_ITEM.finditer(tail)
    }
    expected = {
        "passed": totals["PASSED"],
        "failed": totals["FAILED"],
        "error": totals["ERROR"],
        "xfailed": totals["XFAIL"],
        "xpassed": totals["XPASS"],
        "skipped": totals["SKIPPED"],
    }
    for status, reported in summary.items():
        if expected.get(status, 0) != reported:
            problems.append(
                f"итоговая строка pytest говорит {status}={reported}, "
                f"разбор даёт {expected.get(status, 0)}"
            )

    return {
        "ok": not problems,
        "collected": collected,
        "parsed": len(tests),
        "summary_line": tail,
        "summary_parsed": summary,
        "problems": problems,
    }


# ------------------------------------------------------------------ шаг 2: разбор


def step2(stamp: str) -> None:
    """Разобрать сырой вывод: итоги и разбивка по файлам.

    Артефакт: `reports/steps/<stamp>-02-parsed.json`.
    """
    raw_path = RAW_DIR / f"{stamp}.txt"
    if not raw_path.exists():
        raise SystemExit(f"нет {raw_path.relative_to(REPO_ROOT)} — шаг 1 не выполнен")

    raw_text = raw_path.read_text(encoding="utf-8")
    tests: dict[str, str] = {}
    per_file: dict[str, dict[str, int]] = {}
    for line in raw_text.splitlines():
        match = LINE.match(line.strip())
        if not match:
            continue
        node = f"{match['file']}::{match['test']}"
        status = match["status"]
        tests[node] = status
        bucket = per_file.setdefault(match["file"], {key: 0 for key in STATUSES})
        bucket[status] += 1

    totals = {key: sum(1 for value in tests.values() if value == key) for key in STATUSES}
    consistency = _check_consistency(raw_text, tests, totals)

    write_json(
        STEPS_DIR / f"{stamp}-02-parsed.json",
        {
            "step": 2,
            "name": "разбор",
            "stamp": stamp,
            "total": len(tests),
            "totals": totals,
            "per_file": per_file,
            "consistency": consistency,
            "tests": tests,
        },
    )
    if not consistency["ok"]:
        raise SystemExit(
            "шаг 2: разбор не сошёлся с самим pytest — "
            + "; ".join(consistency["problems"])
            + ". Артефакт записан, но доверять ему нельзя: числа в отчёте были бы занижены."
        )
    print(
        f"шаг 2: всего {len(tests)}, прошло {totals['PASSED']}, упало "
        f"{totals['FAILED'] + totals['ERROR']}, xfail {totals['XFAIL']}, xpass {totals['XPASS']}"
    )


# ------------------------------------------------------------------ шаг 3: сравнение


def step3(stamp: str) -> None:
    """Сравнить с предыдущим прогоном.

    Артефакт: `reports/steps/<stamp>-03-compare.json`.
    Ищет ближайший предыдущий разбор по имени файла. Если его нет — так и пишет,
    и это не ошибка: у первого прогона сравнивать не с чем.
    """
    current = read_json(STEPS_DIR / f"{stamp}-02-parsed.json")
    previous_files = sorted(
        path for path in STEPS_DIR.glob("*-02-parsed.json") if path.name < f"{stamp}-02-parsed.json"
    )

    payload: dict = {"step": 3, "name": "сравнение", "stamp": stamp}
    if not previous_files:
        payload |= {"previous": None, "note": "предыдущих прогонов нет, сравнивать не с чем"}
        write_json(STEPS_DIR / f"{stamp}-03-compare.json", payload)
        print("шаг 3: предыдущих прогонов нет")
        return

    previous = read_json(previous_files[-1])
    before, after = previous["tests"], current["tests"]

    fixed = sorted(n for n in after if n in before and before[n] in ("FAILED", "ERROR") and after[n] == "PASSED")
    broken = sorted(n for n in after if n in before and before[n] == "PASSED" and after[n] in ("FAILED", "ERROR"))
    new_xpass = sorted(n for n in after if after[n] == "XPASS" and before.get(n) != "XPASS")
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))

    payload |= {
        "previous": previous["stamp"],
        "fixed": fixed,
        "broken": broken,
        "new_xpass": new_xpass,
        "added": added,
        "removed": removed,
        "totals_before": previous["totals"],
        "totals_after": current["totals"],
    }
    write_json(STEPS_DIR / f"{stamp}-03-compare.json", payload)
    print(
        f"шаг 3: против {previous['stamp']} — починено {len(fixed)}, сломано {len(broken)}, "
        f"новых XPASS {len(new_xpass)}, добавлено {len(added)}, убрано {len(removed)}"
    )


def _coverage(bug_id: str, test_sources: dict[str, str]) -> dict:
    """Найти упоминания бага в тестах и показать, в каком именно контексте.

    Вход: идентификатор бага и исходники тестов.
    Выход: список файлов, список строк-упоминаний и признак `has_live_test`.

    Признак намеренно слабый и таким остаётся: это поиск подстроки, а не анализ
    того, что тест действительно проверяет. Упоминание бага в тексте `reason=`
    у чужой пометки `xfail` тоже засчитается. Поэтому вместе с признаком
    возвращаются сами строки — чтобы проверяющий агент видел, на чём основан
    вывод, и мог его оспорить. Ограничение описано в `workflow/README.md`.
    """
    files: list[str] = []
    mentions: list[str] = []
    for name, source in sorted(test_sources.items()):
        hits = [line.strip() for line in source.splitlines() if bug_id in line]
        if hits:
            files.append(name)
            mentions += [f"{name}: {hit}" for hit in hits]
    return {"tests_referencing": files, "mentions": mentions, "has_live_test": bool(files)}


# ------------------------------------------------------------------ шаг 4: findings


def step4(stamp: str) -> None:
    """Проверить, что у каждого открытого бага из findings.md есть живой тест.

    Артефакт: `reports/steps/<stamp>-04-findings.json`.

    Открытым считается баг, в карточке которого нет слова «Устранён». Живым —
    тот, чей идентификатор `BUG-NN` встречается в файлах `tests/`. Шаг только
    собирает факты; решать, достаточно ли этого, — работа проверяющего агента.
    """
    findings_path = REPO_ROOT / "findings.md"
    if not findings_path.exists():
        raise SystemExit("нет findings.md — проверять нечего")

    text = findings_path.read_text(encoding="utf-8")
    test_sources = {
        path.name: path.read_text(encoding="utf-8") for path in (REPO_ROOT / "tests").glob("*.py")
    }

    bugs: list[dict] = []
    for match in re.finditer(r"^#{2,3}\s+(BUG-\d+)\s*[—-]\s*(.+)$", text, re.MULTILINE):
        bug_id, title = match.group(1), match.group(2).strip()
        body = text[match.end(): text.find("\n## ", match.end()) if "\n## " in text[match.end():] else len(text)]
        resolved = "Устранён" in body[:600]
        bugs.append(
            {
                "id": bug_id,
                "title": title,
                "status": "устранён" if resolved else "открыт",
                **_coverage(bug_id, test_sources),
            }
        )

    for match in re.finditer(r"^\|\s*(BUG-\d+)\s*\|\s*([^|]+)\|", text, re.MULTILINE):
        bug_id, title = match.group(1), match.group(2).strip()
        if any(bug["id"] == bug_id for bug in bugs):
            continue
        bugs.append(
            {"id": bug_id, "title": title, "status": "открыт", **_coverage(bug_id, test_sources)}
        )

    bugs.sort(key=lambda bug: bug["id"])
    uncovered = [bug["id"] for bug in bugs if bug["status"] == "открыт" and not bug["has_live_test"]]
    write_json(
        STEPS_DIR / f"{stamp}-04-findings.json",
        {
            "step": 4,
            "name": "проверка findings.md",
            "stamp": stamp,
            "bugs": bugs,
            "open_count": sum(1 for bug in bugs if bug["status"] == "открыт"),
            "open_without_test": uncovered,
        },
    )
    print(f"шаг 4: багов {len(bugs)}, открытых без теста {len(uncovered)}: {uncovered or '—'}")


# ------------------------------------------------------------------ шаг 5: отчёт


def step5(stamp: str) -> None:
    """Собрать `reports/report-<stamp>.md` из артефактов шагов 1–4.

    Артефакт: сам отчёт и `reports/steps/<stamp>-05-report.json`.
    Финальная строка (шаг 6) сюда не пишется — её добавляет проверяющий агент.
    """
    run = read_json(STEPS_DIR / f"{stamp}-01-run.json")
    parsed = read_json(STEPS_DIR / f"{stamp}-02-parsed.json")
    compare = read_json(STEPS_DIR / f"{stamp}-03-compare.json")
    findings = read_json(STEPS_DIR / f"{stamp}-04-findings.json")

    totals = parsed["totals"]
    failed = totals["FAILED"] + totals["ERROR"]
    lines = [
        f"# Регрессионный прогон — {stamp}",
        "",
        "Собран автоматически: `workflow/run_regression.py`, шаги 1–5.",
        "Проверка результата и финальная строка — отдельным субагентом, см. раздел «Вердикт».",
        "",
        "## 1. Прогон",
        "",
        f"* команда: `{run['command']}`",
        f"* код возврата: `{run['exit_code']}`",
        f"* длительность: {run['duration_seconds']} с",
        f"* сырой вывод: [`{run['raw_output']}`](raw/{Path(run['raw_output']).name})"
        f" ({run['raw_bytes']} байт)",
        f"* разбор сошёлся с pytest: "
        f"{'да' if parsed.get('consistency', {}).get('ok') else '**НЕТ**'}"
        f" (собрано {parsed.get('consistency', {}).get('collected')}, "
        f"разобрано {parsed.get('consistency', {}).get('parsed')})",
        "",
        "## 2. Итоги",
        "",
        "| Показатель | Значение |",
        "|---|---:|",
        f"| Всего тестов | {parsed['total']} |",
        f"| Прошло | {totals['PASSED']} |",
        f"| Упало | {failed} |",
        f"| Ожидаемо упало (xfail) | {totals['XFAIL']} |",
        f"| Неожиданно прошло (XPASS) | {totals['XPASS']} |",
        f"| Пропущено | {totals['SKIPPED']} |",
        "",
        "### Разбивка по файлам",
        "",
        "| Файл | Всего | Прошло | Упало | xfail | XPASS |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in sorted(parsed["per_file"]):
        bucket = parsed["per_file"][name]
        total = sum(bucket.values())
        lines.append(
            f"| `{name}` | {total} | {bucket['PASSED']} | "
            f"{bucket['FAILED'] + bucket['ERROR']} | {bucket['XFAIL']} | {bucket['XPASS']} |"
        )

    lines += ["", "## 3. Сравнение с предыдущим прогоном", ""]
    if compare.get("previous") is None:
        lines.append("Предыдущих прогонов нет — сравнивать не с чем. Это базовый прогон.")
    else:
        lines += [
            f"Предыдущий прогон: `{compare['previous']}`.",
            "",
            "| Изменение | Количество | Тесты |",
            "|---|---:|---|",
        ]
        for label, key in (
            ("Починилось", "fixed"),
            ("Сломалось", "broken"),
            ("Новых XPASS", "new_xpass"),
            ("Добавлено тестов", "added"),
            ("Убрано тестов", "removed"),
        ):
            items = compare[key]
            shown = ", ".join(f"`{item.split('::')[-1]}`" for item in items[:5]) or "—"
            if len(items) > 5:
                shown += f" и ещё {len(items) - 5}"
            lines.append(f"| {label} | {len(items)} | {shown} |")
        if compare["new_xpass"]:
            lines += [
                "",
                "> **XPASS обнаружен.** Тест, помеченный `xfail`, неожиданно прошёл. "
                "Это значит, что дефект починили, не сняв пометку, — либо тест перестал "
                "проверять то, что должен.",
            ]

    lines += ["", "## 4. Открытые баги и их тесты", "", "| Баг | Статус | Тесты | Живой тест |",
              "|---|---|---|:--:|"]
    for bug in findings["bugs"]:
        tests = ", ".join(f"`{name}`" for name in bug["tests_referencing"]) or "—"
        mark = "да" if bug["has_live_test"] else "**НЕТ**"
        lines.append(f"| {bug['id']} | {bug['status']} | {tests} | {mark} |")
    if findings["open_without_test"]:
        lines += [
            "",
            "> **Открытые баги без теста:** "
            + ", ".join(findings["open_without_test"])
            + ". Дефект, зафиксированный только текстом, при следующей правке кода "
            "не подаст никакого сигнала.",
        ]

    lines += ["", "## Вердикт", "", "_Заполняется проверяющим субагентом (шаг 6)._", ""]

    report_path = REPORTS / f"report-{stamp}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(
        STEPS_DIR / f"{stamp}-05-report.json",
        {
            "step": 5,
            "name": "сборка отчёта",
            "stamp": stamp,
            "report": str(report_path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "report_bytes": report_path.stat().st_size,
        },
    )
    print(f"шаг 5: {report_path.relative_to(REPO_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=["step1", "step2", "step3", "step4", "step5"])
    parser.add_argument("--stamp", help="отметка прогона; для step1 создаётся автоматически")
    args = parser.parse_args()

    stamp = args.stamp or (stamp_now() if args.step == "step1" else None)
    if stamp is None:
        raise SystemExit("--stamp обязателен для всех шагов, кроме step1")

    {"step1": step1, "step2": step2, "step3": step3, "step4": step4, "step5": step5}[args.step](stamp)
    if args.step == "step1":
        print(f"STAMP={stamp}")


if __name__ == "__main__":
    sys.exit(main())
