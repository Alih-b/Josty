"""Link-dense tables survive extraction (#128). Hermetic: no network."""

from __future__ import annotations

from josty.fetch import extract

PARAGRAPH = (
    "<p>"
    + "The award has been given every year since the first ceremony and the list "
    "below names each winner. " * 6
    + "</p>"
)


def _page(table: str) -> str:
    return (
        "<html><head><title>Awards</title></head><body><article><h1>Awards</h1>"
        f"{PARAGRAPH}<h2>Winners</h2>{table}{PARAGRAPH}</article></body></html>"
    )


def _linked_table(rows: int) -> str:
    body = "".join(
        f'<tr><td><a href="/y{i}">{1990 + i}</a></td><td><a href="/f{i}">Film number {i}</a></td>'
        f'<td><a href="/p{i}">Writer {i}</a></td></tr>'
        for i in range(rows)
    )
    return f"<table><tr><th>Year</th><th>Film</th><th>Writer</th></tr>{body}</table>"


def _table_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("|"))


def test_a_table_of_links_keeps_every_row():
    text, method = extract(_page(_linked_table(20)), "https://example.org/awards")

    assert method == "trafilatura"
    assert _table_lines(text) >= 21
    assert "Film number 0" in text and "Film number 19" in text
    assert "| 2009 | Film number 19 | Writer 19 |" in text


def test_links_outside_tables_are_kept():
    html = _page(_linked_table(20)).replace(
        "<h2>Winners</h2>", '<h2>Winners</h2><p>See <a href="/rules">the rules</a>.</p>'
    )

    text, _ = extract(html, "https://example.org/awards")

    assert "[the rules](https://example.org/rules)" in text


def test_a_table_with_few_rows_gets_no_second_pass():
    import trafilatura

    html = _page(_linked_table(3))
    plain = trafilatura.extract(
        html, url="https://example.org/awards", include_links=True, output_format="markdown"
    )

    text, method = extract(html, "https://example.org/awards")

    assert method == "trafilatura"
    assert text == plain.strip()


def test_a_page_whose_table_is_already_kept_is_unchanged():
    rows = "".join(f"<tr><td>{1990 + i}</td><td>Film number {i}</td></tr>" for i in range(10))
    html = _page(f"<table><tr><th>Year</th><th>Film</th></tr>{rows}</table>")

    text, _ = extract(html, "https://example.org/awards")

    assert _table_lines(text) == 12
