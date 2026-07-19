from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from editorial_watch.main import (
    NewsItem,
    ResolvedPaper,
    build_editorial_blocks,
    calculate_window_start,
    choose_unambiguous_recent_paper,
    extract_dois_from_html,
    parse_feed_xml,
)
from paper_watch.models import Paper


def make_paper(doi: str, publication_date: str) -> Paper:
    return Paper(
        openalex_id="",
        doi=doi,
        title="A molecular structure",
        authors=["A. Author", "B. Author"],
        journal="Journal of Test Chemistry",
        publication_date=publication_date,
        abstract_original="An abstract.",
        landing_page_url=f"https://doi.org/{doi}",
        summary_japanese="分子構造とその特性を明らかにした。",
    )


def test_window_start_is_later_of_previous_check_and_72_hour_cutoff() -> None:
    now = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    previous = (now - timedelta(hours=24)).isoformat()
    assert calculate_window_start(now, previous, 72) == now - timedelta(hours=24)
    assert calculate_window_start(now, "", 72) == now - timedelta(hours=72)


def test_parse_rss_feed() -> None:
    payload = b"""<?xml version="1.0"?>
    <rss><channel><item>
      <title>New chemistry paper</title>
      <link>https://example.com/article</link>
      <pubDate>Sun, 19 Jul 2026 21:00:00 GMT</pubDate>
    </item></channel></rss>"""
    items = parse_feed_xml(payload, "Chemistry World")
    assert items == [
        NewsItem(
            source="Chemistry World",
            title="New chemistry paper",
            url="https://example.com/article",
            published_at=datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc),
        )
    ]


def test_extract_dois_deduplicates_and_trims_punctuation() -> None:
    document = """
    <html><body><article>
      <a href="https://doi.org/10.1021/jacs.6c02601">paper</a>
      References: DOI: 10.1021/jacs.6c02601.
      Historical DOI: 10.1021/ja02049a006)
    </article></body></html>
    """
    assert extract_dois_from_html(document) == [
        "10.1021/jacs.6c02601",
        "10.1021/ja02049a006",
    ]


def test_choose_only_unambiguous_recent_original() -> None:
    article_date = datetime(2026, 7, 20, tzinfo=timezone.utc)
    recent = ResolvedPaper(
        make_paper("10.1000/recent", "2026-07-10"),
        date(2026, 7, 10),
    )
    historical = ResolvedPaper(
        make_paper("10.1000/old", "1900-01-01"),
        date(1900, 1, 1),
    )
    selected = choose_unambiguous_recent_paper(
        [recent, historical],
        article_date,
        publication_match_days=180,
    )
    assert selected is recent.paper

    second_recent = ResolvedPaper(
        make_paper("10.1000/recent2", "2026-07-11"),
        date(2026, 7, 11),
    )
    assert (
        choose_unambiguous_recent_paper(
            [recent, second_recent],
            article_date,
            publication_match_days=180,
        )
        is None
    )


def test_editorial_blocks_match_regular_format_without_score() -> None:
    paper = make_paper("10.1000/test", "2026-07-10")
    blocks = build_editorial_blocks(paper, {"Chemistry World", "C&EN"})
    header = blocks[0]["text"]["text"]
    assert header.startswith("*【Chemistry World・C&amp;EN掲載】")
    assert "Journal of Test Chemistry | 2026-07-10" in header
    assert "Score:" not in header
    assert "Matched keywords:" not in header
