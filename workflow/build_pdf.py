"""Сборка PDF-отчёта из HTML средствами печати браузера.

Конвейер: Playwright управляет **уже установленным** Chrome (канал `chrome`),
свой браузер не скачивается. Chrome через `--print-to-pdf` колонтитулы не умеет,
поэтому используется протокол печати с шаблонами — только так в PDF попадают
номера страниц.

Две вещи, которых не даёт печать «в один проход»:

1. **Титул не должен нести колонтитулы.** Документ печатается двумя заходами
   и склеивается: первая страница без колонтитулов, остальные с ними.
2. **Номера в оглавлении должны быть настоящими.** После первой печати из PDF
   извлекается текст каждой страницы, по нему определяется, на какой странице
   начинается раздел, числа подставляются в HTML, и документ печатается заново.

Запуск из корня репозитория:

    .venv\\Scripts\\python.exe workflow\\build_pdf.py reports\\regression-report.html
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Колонтитулы. Chrome требует инлайновых стилей: внешние сюда не доезжают.
HEADER = """
<div style="width:100%;font-size:7pt;color:#9a938c;
            font-family:'Segoe UI',Arial,sans-serif;padding:0 22mm;">
  <div style="border-bottom:0.5pt solid #d9d2c9;padding-bottom:2.5mm;
              display:flex;justify-content:space-between;">
    <span>Регресс кредитного калькулятора</span>
    <span>Вариант B4</span>
  </div>
</div>
"""

FOOTER = """
<div style="width:100%;font-size:8pt;color:#6f6a64;
            font-family:'Segoe UI',Arial,sans-serif;padding:0 22mm;">
  <div style="border-top:0.5pt solid #d9d2c9;padding-top:2.5mm;text-align:right;">
    <span class="pageNumber"></span>
  </div>
</div>
"""

BLANK = "<div></div>"


def _print(page, path: Path, *, ranges: str, with_chrome: bool) -> None:
    """Напечатать диапазон страниц в файл.

    Вход: страница Playwright, путь, диапазон вида `"1"` или `"2-"`,
    признак «печатать колонтитулы».
    Выход: `None`; файл создаётся по указанному пути.

    Поля одинаковы для обоих заходов, и это принципиально: Chrome разбивает
    документ на страницы заново при каждой печати, и при разных полях диапазон
    `"1"` одного захода и `"2-"` другого относились бы к разным разбиениям.
    Титул отличается только тем, что колонтитулы на нём не печатаются.
    """
    page.pdf(
        path=str(path),
        format="A4",
        print_background=True,
        page_ranges=ranges,
        display_header_footer=True,
        header_template=HEADER if with_chrome else BLANK,
        footer_template=FOOTER if with_chrome else BLANK,
        margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
    )


def _page_texts(pdf_path: Path) -> list[str]:
    """Извлечь текст каждой страницы PDF.

    Вход: путь к PDF. Выход: список строк по числу страниц.
    Нужен, чтобы посчитать реальные номера страниц для оглавления.
    """
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        return [document[i].get_textpage().get_text_range() for i in range(len(document))]
    finally:
        document.close()


def _toc_numbers(texts: list[str], titles: list[str]) -> dict[str, int]:
    """Сопоставить заголовкам разделов номера страниц.

    Вход: тексты страниц и подписи из атрибутов `data-toc`.
    Выход: словарь «подпись → номер страницы».

    Оглавление пропускается: иначе заголовок нашёлся бы на нём самом.
    """
    numbers: dict[str, int] = {}
    for title in titles:
        needle = re.sub(r"\s+", "", title).lower()
        for index, text in enumerate(texts, start=1):
            if "Содержание" in text:
                continue
            if needle in re.sub(r"\s+", "", text).lower():
                numbers[title] = index
                break
    return numbers


def _patch_toc(html: str, numbers: dict[str, int]) -> str:
    """Подставить вычисленные номера страниц в оглавление.

    Вход: исходный HTML и словарь номеров. Выход: HTML с числами.
    """
    def replace(match: re.Match) -> str:
        title = match.group("title")
        number = numbers.get(title)
        return f'{match.group("open")}стр. {number}</span>' if number else match.group(0)

    return re.sub(
        r'(?P<open><span class="toc-note" data-toc="(?P<title>[^"]+)">)[^<]*</span>',
        replace,
        html,
    )


def build(html_path: Path) -> Path:
    """Собрать PDF: измерить страницы, проставить оглавление, склеить.

    Вход: путь к HTML. Выход: путь к готовому PDF рядом с исходником.
    """
    pdf_path = html_path.with_suffix(".pdf")
    original = html_path.read_text(encoding="utf-8")
    titles = re.findall(r'data-toc="([^"]+)"', original)

    with tempfile.TemporaryDirectory() as workdir:
        temp = Path(workdir)
        probe, cover, body = temp / "probe.pdf", temp / "cover.pdf", temp / "body.pdf"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome")
            try:
                page = browser.new_page()

                # Проход 1: измерить, на какой странице какой раздел.
                page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
                page.emulate_media(media="print")
                _print(page, probe, ranges="", with_chrome=True)
                numbers = _toc_numbers(_page_texts(probe), titles)

                # Проход 2: печать с настоящими номерами в оглавлении.
                patched = temp / html_path.name
                patched.write_text(_patch_toc(original, numbers), encoding="utf-8")
                page.goto(patched.resolve().as_uri(), wait_until="networkidle")
                page.emulate_media(media="print")
                _print(page, cover, ranges="1", with_chrome=False)
                _print(page, body, ranges="2-", with_chrome=True)
            finally:
                browser.close()

        # Исходники держат дескрипторы файлов и должны быть закрыты до того,
        # как временный каталог начнёт удаляться: на Windows иначе PermissionError.
        merged = pdfium.PdfDocument.new()
        parts = [pdfium.PdfDocument(str(part)) for part in (cover, body)]
        try:
            for part in parts:
                merged.import_pages(part)
            merged.save(str(pdf_path))
        finally:
            merged.close()
            for part in parts:
                part.close()

    return pdf_path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("укажите путь к HTML: build_pdf.py reports\\regression-report.html")
    source = Path(sys.argv[1]).resolve()
    if not source.exists():
        raise SystemExit(f"нет файла {source}")
    result = build(source)
    shown = result.relative_to(REPO_ROOT) if result.is_relative_to(REPO_ROOT) else result
    pages = len(pdfium.PdfDocument(str(result)))
    print(f"собрано: {shown}, страниц {pages}, {result.stat().st_size} байт")


if __name__ == "__main__":
    main()
