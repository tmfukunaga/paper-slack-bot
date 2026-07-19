from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import requests
import yaml
from bs4 import BeautifulSoup
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from paper_watch.ai_summary import OpenAISummarizer, SummaryError
from paper_watch.crossref_client import fetch_abstract
from paper_watch.models import Paper
from paper_watch.selection import is_excluded_source
from paper_watch.text_utils import (
    escape_slack_mrkdwn,
    normalize_doi,
    reconstruct_abstract,
    strip_markup,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WATCH_CONFIG = ROOT / "editorial_watch.yaml"
DEFAULT_SUMMARY_CONFIG = ROOT / "config.yaml"
DEFAULT_STATE = ROOT / "data" / "editorial-watch-state.json"
OPENALEX = "https://api.openalex.org/works"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
DOI_END = ".,;:)]}>\"'’”"


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    url: str
    published_at: datetime


@dataclass(frozen=True)
class ResolvedPaper:
    paper: Paper
    publication_date: date


@dataclass
class PendingPaper:
    paper: Paper
    sources: set[str] = field(default_factory=set)
    article_urls: set[str] = field(default_factory=set)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--watch-config", type=Path, default=DEFAULT_WATCH_CONFIG)
    parser.add_argument("--summary-config", type=Path, default=DEFAULT_SUMMARY_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    return parser.parse_args()


def parse_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        result = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def calculate_window_start(
    now: datetime, last_successful_check: str, lookback_hours: int
) -> datetime:
    cutoff = now - timedelta(hours=lookback_hours)
    previous = parse_datetime(last_successful_check)
    return max(cutoff, previous) if previous else cutoff


def load_state(path: Path) -> dict[str, Any]:
    default = {"last_successful_check": "", "posted": {}, "processed_articles": {}}
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        value = {}
    for key, fallback in default.items():
        value.setdefault(key, fallback.copy() if isinstance(fallback, dict) else fallback)
    return value


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def prune_state(state: dict[str, Any], now: datetime) -> None:
    cutoff = now - timedelta(days=180)
    for bucket, timestamp_key in (
        ("posted", "posted_at"),
        ("processed_articles", "processed_at"),
    ):
        kept = {}
        for key, record in state.get(bucket, {}).items():
            if not isinstance(record, dict):
                continue
            stamp = parse_datetime(str(record.get(timestamp_key, "")))
            if stamp and stamp >= cutoff:
                kept[key] = record
        state[bucket] = kept


def _name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(element: ET.Element, names: set[str]) -> str:
    for child in element:
        if _name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def _link(element: ET.Element) -> str:
    for child in element:
        if _name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if href:
            return href
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def parse_feed_xml(payload: bytes | str, source: str) -> list[NewsItem]:
    root = ET.fromstring(payload)
    items: list[NewsItem] = []
    seen: set[str] = set()
    for element in root.iter():
        if _name(element.tag) not in {"item", "entry"}:
            continue
        title = _text(element, {"title"})
        url = _link(element)
        published = parse_datetime(
            _text(element, {"pubdate", "published", "updated", "date"})
        )
        if title and url and published and url not in seen:
            seen.add(url)
            items.append(NewsItem(source, strip_markup(title), url, published))
    return items


def make_session(contact_email: str) -> requests.Session:
    session = requests.Session()
    identity = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 paper-slack-bot/1.0"
    )
    if contact_email:
        identity += f" (mailto:{contact_email})"
    session.headers.update(
        {
            "User-Agent": identity,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return session


def get(session: requests.Session, url: str, timeout: float) -> requests.Response:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response


def extract_dois_from_html(document: str) -> list[str]:
    soup = BeautifulSoup(document, "html.parser")
    candidates: list[str] = []
    for tag in soup.select("a[href]"):
        href = unquote(str(tag.get("href") or ""))
        if "doi.org/" in href.lower():
            candidates.extend(DOI_RE.findall(href))
    candidates.extend(DOI_RE.findall(soup.get_text(" ", strip=True)))
    result: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        doi = normalize_doi(raw.rstrip(DOI_END))
        if doi and doi not in seen:
            seen.add(doi)
            result.append(doi)
    return result


def _date_from_work(work: dict[str, Any]) -> str:
    return str(work.get("publication_date") or work.get("publication_year") or "").strip()


def _paper_from_openalex(work: dict[str, Any], doi: str) -> Paper | None:
    title = strip_markup(str(work.get("title") or work.get("display_name") or ""))
    if not title:
        return None
    authors = [
        str(authorship.get("author", {}).get("display_name") or "").strip()
        for authorship in work.get("authorships", []) or []
        if authorship.get("author", {}).get("display_name")
    ]
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    return Paper(
        openalex_id=str(work.get("id") or ""),
        doi=doi,
        title=title,
        authors=authors,
        journal=strip_markup(str(source.get("display_name") or "")),
        publication_date=_date_from_work(work),
        abstract_original=reconstruct_abstract(work.get("abstract_inverted_index")),
        landing_page_url=f"https://doi.org/{doi}",
    )


def fetch_paper(
    session: requests.Session,
    doi: str,
    api_key: str,
    contact_email: str,
    timeout: float,
) -> ResolvedPaper | None:
    params = {"filter": f"doi:{doi}", "per-page": 1}
    if api_key:
        params["api_key"] = api_key
    response = session.get(OPENALEX, params=params, timeout=timeout)
    response.raise_for_status()
    results = response.json().get("results", []) or []
    if not results:
        return None
    work = results[0]
    paper = _paper_from_openalex(work, doi)
    if not paper:
        return None
    if not paper.abstract_original:
        paper.abstract_original = fetch_abstract(doi, contact_email)
    try:
        published = date.fromisoformat(paper.publication_date[:10])
    except ValueError:
        return None
    return ResolvedPaper(paper, published)


def choose_unambiguous_recent_paper(
    resolved: list[ResolvedPaper],
    article_date: datetime,
    publication_match_days: int,
) -> Paper | None:
    article_day = article_date.date()
    candidates = [
        item
        for item in resolved
        if article_day - timedelta(days=publication_match_days)
        <= item.publication_date
        <= article_day + timedelta(days=14)
    ]
    unique = {item.paper.key: item.paper for item in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


def source_label(sources: set[str]) -> str:
    order = ["Chemistry World", "C&EN"]
    names = [name for name in order if name in sources]
    names.extend(sorted(sources - set(names)))
    return "・".join(names) + "掲載"


def _author_line(authors: list[str]) -> str:
    if not authors:
        return "著者情報なし"
    shown = authors[:8]
    suffix = ", et al." if len(authors) > 8 else ""
    return escape_slack_mrkdwn(", ".join(shown) + suffix)


def build_editorial_blocks(
    paper: Paper,
    sources: set[str],
    article_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    summary = paper.summary_japanese or "Summary was unavailable."
    label = escape_slack_mrkdwn(source_label(sources))
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*【{label}】{escape_slack_mrkdwn(paper.title)}*\n"
                    f"{_author_line(paper.authors)}\n"
                    f"{escape_slack_mrkdwn(paper.journal or '雑誌名不明')} | "
                    f"{escape_slack_mrkdwn(paper.publication_date or '公開日不明')}"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*要約*\n{escape_slack_mrkdwn(summary)}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*論文リンク*\nhttps://doi.org/{paper.doi}",
            },
        },
    ]
    if article_urls:
        lines = "\n".join(sorted(article_urls))
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"掲載記事:\n{lines}"}],
            }
        )
    blocks.append({"type": "divider"})
    return blocks


def post_paper(client: WebClient, channel_id: str, pending: PendingPaper) -> str:
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text=(
                f"【{source_label(pending.sources)}】{pending.paper.title} — "
                f"{pending.paper.summary_japanese}"
            ),
            blocks=build_editorial_blocks(
                pending.paper, pending.sources, pending.article_urls
            ),
            unfurl_links=False,
            unfurl_media=False,
        )
    except SlackApiError as exc:
        raise RuntimeError(
            f"Slack rejected DOI={pending.paper.doi}: {exc.response.get('error')}"
        ) from exc
    return str(response.get("ts") or "")


def feeds_from_config(config: dict[str, Any]) -> list[tuple[str, str]]:
    return [(str(item["source"]), str(item["url"])) for item in config.get("feeds", [])]


def collect_news(
    session: requests.Session,
    feeds: list[tuple[str, str]],
    window_start: datetime,
    now: datetime,
    timeout: float,
) -> list[NewsItem]:
    items: dict[tuple[str, str], NewsItem] = {}
    for source, url in feeds:
        response = get(session, url, timeout)
        for item in parse_feed_xml(response.content, source):
            if window_start < item.published_at <= now:
                items[(item.source, item.url)] = item
    return sorted(items.values(), key=lambda item: item.published_at)


def resolve_news_item(
    session: requests.Session,
    item: NewsItem,
    *,
    api_key: str,
    contact_email: str,
    timeout: float,
    publication_match_days: int,
) -> Paper | None:
    article = get(session, item.url, timeout)
    dois = extract_dois_from_html(article.text)
    resolved: list[ResolvedPaper] = []
    for doi in dois:
        try:
            result = fetch_paper(session, doi, api_key, contact_email, timeout)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            raise
        if result:
            resolved.append(result)
    return choose_unambiguous_recent_paper(
        resolved, item.published_at, publication_match_days
    )


def mark_processed(
    state: dict[str, Any],
    item: NewsItem,
    processed_at: str,
) -> None:
    state["processed_articles"][item.url] = {
        "processed_at": processed_at,
        "source": item.source,
        "title": item.title,
    }


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with args.watch_config.open(encoding="utf-8") as handle:
        watch_config = yaml.safe_load(handle)
    with args.summary_config.open(encoding="utf-8") as handle:
        summary_config = yaml.safe_load(handle)

    now = datetime.now(timezone.utc)
    state = load_state(args.state)
    prune_state(state, now)
    window_start = calculate_window_start(
        now,
        str(state.get("last_successful_check", "")),
        int(watch_config["lookback_hours"]),
    )
    timeout = float(watch_config["request_timeout_seconds"])
    contact_email = os.getenv("CONTACT_EMAIL", "").strip()
    openalex_key = os.getenv("OPENALEX_API_KEY", "").strip()
    session = make_session(contact_email)

    news_items = collect_news(
        session,
        feeds_from_config(watch_config),
        window_start,
        now,
        timeout,
    )
    pending: dict[str, PendingPaper] = {}
    processed_at = now.replace(microsecond=0).isoformat()

    for item in news_items:
        if item.url in state["processed_articles"]:
            continue
        paper = resolve_news_item(
            session,
            item,
            api_key=openalex_key,
            contact_email=contact_email,
            timeout=timeout,
            publication_match_days=int(watch_config["publication_match_days"]),
        )
        if not paper:
            mark_processed(state, item, processed_at)
            continue
        if paper.key in state["posted"]:
            mark_processed(state, item, processed_at)
            continue
        if is_excluded_source(paper, summary_config):
            logging.info("Rejected excluded source DOI=%s", paper.doi)
            mark_processed(state, item, processed_at)
            continue
        if not paper.abstract_original.strip():
            logging.info("No abstract; skipping DOI=%s", paper.doi)
            mark_processed(state, item, processed_at)
            continue
        current = pending.setdefault(paper.key, PendingPaper(paper))
        current.sources.add(item.source)
        current.article_urls.add(item.url)

    if args.dry_run:
        print(
            json.dumps(
                [
                    {
                        "doi": item.paper.doi,
                        "title": item.paper.title,
                        "sources": sorted(item.sources),
                        "article_urls": sorted(item.article_urls),
                    }
                    for item in pending.values()
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    slack_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    channel_id = os.getenv("SLACK_CHANNEL_ID", "").strip()
    if not all((openai_key, slack_token, channel_id)):
        raise RuntimeError(
            "OPENAI_API_KEY, SLACK_BOT_TOKEN, and SLACK_CHANNEL_ID are required."
        )

    summarizer = OpenAISummarizer(openai_key, summary_config)
    slack = WebClient(token=slack_token)
    failed_summaries: list[str] = []
    for item in pending.values():
        try:
            summary = summarizer.summarize(
                item.paper.title,
                item.paper.abstract_original,
                item.paper.doi,
            )
        except SummaryError as exc:
            logging.error("Summary failed DOI=%s: %s", item.paper.doi, exc)
            failed_summaries.append(item.paper.doi)
            continue
        item.paper.summary_japanese = summary.text
        item.paper.summary_language = summary.language
        item.paper.summary_model = summary.model
        timestamp = post_paper(slack, channel_id, item)
        posted_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        state["posted"][item.paper.key] = {
            "posted_at": posted_at,
            "slack_ts": timestamp,
            "title": item.paper.title,
            "doi": item.paper.doi,
            "sources": sorted(item.sources),
        }
        for article_url in item.article_urls:
            state["processed_articles"][article_url] = {
                "processed_at": posted_at,
                "source": source_label(item.sources),
                "title": item.paper.title,
            }
        save_state(args.state, state)
        time.sleep(float(watch_config["slack_pause_seconds"]))

    if failed_summaries:
        save_state(args.state, state)
        raise RuntimeError(
            "Summary failed for DOI(s): " + ", ".join(failed_summaries)
        )

    state["last_successful_check"] = now.replace(microsecond=0).isoformat()
    save_state(args.state, state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Editorial paper watch failed")
        raise
