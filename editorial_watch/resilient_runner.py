from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any

import requests
import yaml
from slack_sdk import WebClient

from editorial_watch import main as editorial_main
from paper_watch.ai_summary import OpenAISummarizer, SummaryError
from paper_watch.models import Paper
from paper_watch.selection import is_excluded_source


class RetryableResolutionError(RuntimeError):
    """An editorial article could not yet be resolved to a paper."""


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _news_item_from_record(
    url: str, record: dict[str, Any]
) -> editorial_main.NewsItem | None:
    published = editorial_main.parse_datetime(str(record.get("published_at", "")))
    source = str(record.get("source", "")).strip()
    title = str(record.get("title", "")).strip()
    if not (url and published and source and title):
        return None
    return editorial_main.NewsItem(source, title, url, published)


def _remember_retry(
    state: dict[str, Any],
    item: editorial_main.NewsItem,
    attempted_at: str,
    error: str,
) -> None:
    retries = state.setdefault("retry_articles", {})
    previous = retries.get(item.url, {})
    retries[item.url] = {
        "source": item.source,
        "title": item.title,
        "published_at": item.published_at.isoformat(),
        "first_failed_at": previous.get("first_failed_at", attempted_at),
        "last_failed_at": attempted_at,
        "attempts": int(previous.get("attempts", 0)) + 1,
        "last_error": error[:1000],
    }


def _clear_retry(state: dict[str, Any], url: str) -> None:
    state.setdefault("retry_articles", {}).pop(url, None)


def collect_news_resilient(
    session: requests.Session,
    feeds: list[tuple[str, str]],
    window_start: datetime,
    now: datetime,
    timeout: float,
    state: dict[str, Any],
) -> tuple[list[editorial_main.NewsItem], bool]:
    items: dict[tuple[str, str], editorial_main.NewsItem] = {}
    feed_failed = False

    for source, url in feeds:
        try:
            response = editorial_main.get(session, url, timeout)
            parsed = editorial_main.parse_feed_xml(response.content, source)
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            feed_failed = True
            logging.error("Feed retrieval failed source=%s url=%s: %s", source, url, exc)
            continue
        for item in parsed:
            if window_start < item.published_at <= now:
                items[(item.source, item.url)] = item

    for url, record in state.setdefault("retry_articles", {}).items():
        if not isinstance(record, dict):
            continue
        item = _news_item_from_record(url, record)
        if item:
            items[(item.source, item.url)] = item

    return sorted(items.values(), key=lambda item: item.published_at), feed_failed


def resolve_from_openalex_title(
    session: requests.Session,
    item: editorial_main.NewsItem,
    *,
    api_key: str,
    contact_email: str,
    timeout: float,
    publication_match_days: int,
) -> Paper | None:
    params: dict[str, Any] = {"search": item.title, "per-page": 25}
    if api_key:
        params["api_key"] = api_key
    response = session.get(editorial_main.OPENALEX, params=params, timeout=timeout)
    response.raise_for_status()

    wanted = _normalized_title(item.title)
    resolved: list[editorial_main.ResolvedPaper] = []
    for work in response.json().get("results", []) or []:
        candidate_title = str(work.get("title") or work.get("display_name") or "")
        if _normalized_title(candidate_title) != wanted:
            continue
        doi = editorial_main.normalize_doi(str(work.get("doi") or ""))
        if not doi:
            continue
        paper = editorial_main._paper_from_openalex(work, doi)
        if not paper:
            continue
        if not paper.abstract_original:
            try:
                paper.abstract_original = editorial_main.fetch_abstract(doi, contact_email)
            except Exception:
                logging.warning("Abstract fallback failed DOI=%s", doi, exc_info=True)
        try:
            published = date.fromisoformat(paper.publication_date[:10])
        except ValueError:
            continue
        resolved.append(editorial_main.ResolvedPaper(paper, published))

    return editorial_main.choose_unambiguous_recent_paper(
        resolved, item.published_at, publication_match_days
    )


def resolve_news_item_resilient(
    session: requests.Session,
    item: editorial_main.NewsItem,
    *,
    api_key: str,
    contact_email: str,
    timeout: float,
    publication_match_days: int,
) -> Paper:
    article_error: Exception | None = None
    try:
        paper = editorial_main.resolve_news_item(
            session,
            item,
            api_key=api_key,
            contact_email=contact_email,
            timeout=timeout,
            publication_match_days=publication_match_days,
        )
        if paper:
            return paper
    except requests.RequestException as exc:
        article_error = exc
        logging.warning(
            "Article retrieval failed; trying exact-title OpenAlex recovery: %s",
            item.url,
        )

    try:
        paper = resolve_from_openalex_title(
            session,
            item,
            api_key=api_key,
            contact_email=contact_email,
            timeout=timeout,
            publication_match_days=publication_match_days,
        )
    except requests.RequestException as exc:
        detail = f"article={article_error!r}; title_fallback={exc!r}"
        raise RetryableResolutionError(detail) from exc

    if paper:
        return paper

    detail = (
        f"article retrieval failed: {article_error!r}"
        if article_error
        else "article contained no uniquely resolvable DOI and exact-title recovery found no unique paper"
    )
    raise RetryableResolutionError(detail)


def main() -> None:
    args = editorial_main.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with args.watch_config.open(encoding="utf-8") as handle:
        watch_config = yaml.safe_load(handle)
    with args.summary_config.open(encoding="utf-8") as handle:
        summary_config = yaml.safe_load(handle)

    now = datetime.now(timezone.utc)
    state = editorial_main.load_state(args.state)
    state.setdefault("retry_articles", {})
    editorial_main.prune_state(state, now)
    window_start = editorial_main.calculate_window_start(
        now,
        str(state.get("last_successful_check", "")),
        int(watch_config["lookback_hours"]),
    )
    timeout = float(watch_config["request_timeout_seconds"])
    contact_email = os.getenv("CONTACT_EMAIL", "").strip()
    openalex_key = os.getenv("OPENALEX_API_KEY", "").strip()
    session = editorial_main.make_session(contact_email)

    news_items, feed_failed = collect_news_resilient(
        session,
        editorial_main.feeds_from_config(watch_config),
        window_start,
        now,
        timeout,
        state,
    )
    pending: dict[str, editorial_main.PendingPaper] = {}
    article_items: dict[str, editorial_main.NewsItem] = {
        item.url: item for item in news_items
    }
    processed_at = now.replace(microsecond=0).isoformat()
    unresolved = False

    for item in news_items:
        if (
            item.url in state["processed_articles"]
            and item.url not in state["retry_articles"]
        ):
            continue
        try:
            paper = resolve_news_item_resilient(
                session,
                item,
                api_key=openalex_key,
                contact_email=contact_email,
                timeout=timeout,
                publication_match_days=int(watch_config["publication_match_days"]),
            )
        except RetryableResolutionError as exc:
            unresolved = True
            _remember_retry(state, item, processed_at, str(exc))
            logging.warning(
                "Queued unresolved article for future retry source=%s url=%s attempts=%s",
                item.source,
                item.url,
                state["retry_articles"][item.url]["attempts"],
            )
            continue

        if paper.key in state["posted"]:
            editorial_main.mark_processed(state, item, processed_at)
            _clear_retry(state, item.url)
            continue
        if is_excluded_source(paper, summary_config):
            logging.info("Rejected excluded source DOI=%s", paper.doi)
            editorial_main.mark_processed(state, item, processed_at)
            _clear_retry(state, item.url)
            continue
        if not paper.abstract_original.strip():
            unresolved = True
            _remember_retry(
                state,
                item,
                processed_at,
                f"abstract unavailable for DOI={paper.doi}",
            )
            logging.warning("Queued paper without abstract DOI=%s", paper.doi)
            continue

        current = pending.setdefault(paper.key, editorial_main.PendingPaper(paper))
        current.sources.add(item.source)
        current.article_urls.add(item.url)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "pending": [
                        {
                            "doi": item.paper.doi,
                            "title": item.paper.title,
                            "sources": sorted(item.sources),
                            "article_urls": sorted(item.article_urls),
                        }
                        for item in pending.values()
                    ],
                    "retry_articles": state["retry_articles"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        editorial_main.save_state(args.state, state)
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

    for item in pending.values():
        try:
            summary = summarizer.summarize(
                item.paper.title,
                item.paper.abstract_original,
                item.paper.doi,
            )
            item.paper.summary_japanese = summary.text
            item.paper.summary_language = summary.language
            item.paper.summary_model = summary.model
            timestamp = editorial_main.post_paper(slack, channel_id, item)
        except (SummaryError, RuntimeError, requests.RequestException) as exc:
            unresolved = True
            logging.error("Editorial post deferred DOI=%s: %s", item.paper.doi, exc)
            for article_url in item.article_urls:
                source_item = article_items.get(article_url)
                if source_item:
                    _remember_retry(state, source_item, processed_at, str(exc))
            continue

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
                "source": editorial_main.source_label(item.sources),
                "title": item.paper.title,
            }
            _clear_retry(state, article_url)
        editorial_main.save_state(args.state, state)
        time.sleep(float(watch_config["slack_pause_seconds"]))

    if not feed_failed and not unresolved:
        state["last_successful_check"] = now.replace(microsecond=0).isoformat()
    else:
        logging.warning(
            "Checkpoint not advanced: feed_failed=%s unresolved=%s retry_queue=%s",
            feed_failed,
            unresolved,
            len(state["retry_articles"]),
        )
    editorial_main.save_state(args.state, state)
