from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import yaml
from slack_sdk import WebClient

from paper_watch.ai_summary import OpenAISummarizer, SummaryError
from paper_watch.models import Paper
from paper_watch.text_utils import escape_slack_mrkdwn

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "trending_watch.yaml"
SCOPUS_SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
SCOPUS_ABSTRACT_URL = "https://api.elsevier.com/content/abstract/doi/{doi}"
MENDELEY_TOKEN_URL = "https://api.mendeley.com/oauth/token"
MENDELEY_CATALOG_URL = "https://api.mendeley.com/catalog"


@dataclass
class Candidate:
    doi: str
    title: str
    authors: list[str]
    journal: str
    publication_date: str
    url: str
    abstract: str = ""
    reader_count: int = 0


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def elsevier_get(
    url: str,
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.get(
        url,
        headers={"X-ELS-APIKey": api_key, "Accept": "application/json"},
        params=params,
        timeout=45,
    )
    if response.status_code in {401, 403}:
        raise RuntimeError(f"Elsevier API access denied for {url}: HTTP {response.status_code}")
    if not response.ok:
        detail = re.sub(r"\s+", " ", response.text).strip()[:500]
        raise RuntimeError(
            f"Elsevier API request failed for {url}: HTTP {response.status_code}: {detail}"
        )
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _quoted_query(field: str, values: list[str]) -> str:
    expressions = []
    for value in values:
        escaped = value.replace('"', '\\"')
        expressions.append(f'{field}("{escaped}")')
    return " OR ".join(expressions)


def build_scopus_query(config: dict[str, Any]) -> str:
    current_year = date.today().year
    dedicated = _quoted_query("SRCTITLE", config["scope"]["dedicated_journals"])
    broad = _quoted_query("SRCTITLE", config["scope"]["broad_journals"])
    title = _quoted_query("TITLE", config["scope"]["title_keywords"])
    scope = f"({dedicated}) OR (({broad}) AND ({title}))"
    return f"SUBJAREA(CHEM) AND PUBYEAR > {current_year - 2} AND ({scope})"


def _within_publication_window(published: str, config: dict[str, Any]) -> bool:
    try:
        published_date = date.fromisoformat(published[:10])
    except ValueError:
        return False
    timezone_name = str(config["retrieval"].get("timezone", "Asia/Tokyo"))
    now = datetime.now(ZoneInfo(timezone_name))
    cutoff = (now - timedelta(hours=int(config["retrieval"]["publication_lookback_hours"]))).date()
    return cutoff <= published_date <= now.date()


def _in_scope(title: str, journal: str, config: dict[str, Any]) -> bool:
    haystack = title.casefold()
    excluded = [str(x).casefold() for x in config["scope"]["excluded_title_keywords"]]
    if any(term in haystack for term in excluded):
        return False
    dedicated = {str(x).casefold() for x in config["scope"]["dedicated_journals"]}
    if journal.casefold() in dedicated:
        return True
    broad = {str(x).casefold() for x in config["scope"]["broad_journals"]}
    keywords = [str(x).casefold() for x in config["scope"]["title_keywords"]]
    return journal.casefold() in broad and any(term in haystack for term in keywords)


def scopus_candidates(api_key: str, config: dict[str, Any]) -> list[Candidate]:
    maximum = int(config["retrieval"]["maximum_candidates"])
    query = build_scopus_query(config)
    entries: list[dict[str, Any]] = []

    for start in range(0, maximum, 25):
        page_size = min(25, maximum - start)
        payload = elsevier_get(
            SCOPUS_SEARCH_URL,
            api_key,
            params={
                "query": query,
                "count": page_size,
                "start": start,
                "sort": "-coverDate",
                "view": "STANDARD",
            },
        )
        page = payload.get("search-results", {}).get("entry", []) or []
        if not isinstance(page, list) or not page:
            break
        entries.extend(item for item in page if isinstance(item, dict))
        if len(page) < page_size:
            break

    results: dict[str, Candidate] = {}
    for entry in entries:
        doi = str(entry.get("prism:doi") or "").strip()
        title = str(entry.get("dc:title") or "").strip()
        journal = str(entry.get("prism:publicationName") or "").strip()
        published = str(entry.get("prism:coverDate") or "").strip()
        if not doi or not title or not journal or not published:
            continue
        if not _within_publication_window(published, config):
            continue
        if not _in_scope(title, journal, config):
            continue
        author = str(entry.get("dc:creator") or "").strip()
        results.setdefault(
            doi.casefold(),
            Candidate(
                doi=doi,
                title=title,
                authors=[author] if author else [],
                journal=journal,
                publication_date=published[:10],
                url=f"https://doi.org/{doi}",
            ),
        )
    return list(results.values())


def mendeley_access_token(client_id: str, client_secret: str) -> str:
    response = requests.post(
        MENDELEY_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": "all"},
        timeout=45,
    )
    if not response.ok:
        detail = re.sub(r"\s+", " ", response.text).strip()[:500]
        raise RuntimeError(
            f"Mendeley token request failed: HTTP {response.status_code}: {detail}"
        )
    payload = response.json()
    token = str(payload.get("access_token") or "").strip() if isinstance(payload, dict) else ""
    if not token:
        raise RuntimeError("Mendeley token response did not contain access_token")
    return token


def mendeley_reader_count(access_token: str, doi: str) -> int:
    response = requests.get(
        MENDELEY_CATALOG_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.mendeley-document.1+json",
        },
        params={"doi": doi, "view": "stats"},
        timeout=45,
    )
    if not response.ok:
        detail = re.sub(r"\s+", " ", response.text).strip()[:500]
        raise RuntimeError(
            f"Mendeley Catalog request failed for DOI={doi}: "
            f"HTTP {response.status_code}: {detail}"
        )
    payload = response.json()
    documents = payload if isinstance(payload, list) else [payload]
    counts: list[int] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        value = document.get("reader_count", 0)
        if isinstance(value, (int, float)):
            counts.append(max(0, int(value)))
    return max(counts, default=0)


def rank_candidates(candidates: list[Candidate], minimum_reader_count: int) -> list[Candidate]:
    usable = [item for item in candidates if item.reader_count >= minimum_reader_count]
    return sorted(
        usable,
        key=lambda item: (item.reader_count, item.publication_date, item.title.casefold()),
        reverse=True,
    )


def fetch_abstract(api_key: str, doi: str) -> tuple[str, list[str]]:
    try:
        payload = elsevier_get(SCOPUS_ABSTRACT_URL.format(doi=quote(doi, safe="/")), api_key)
    except (requests.RequestException, RuntimeError):
        return "", []
    response = payload.get("abstracts-retrieval-response", {}) or {}
    core = response.get("coredata", {}) or {}
    abstract = str(core.get("dc:description") or "").strip()
    authors_value = response.get("authors", {}).get("author", []) or []
    if isinstance(authors_value, dict):
        authors_value = [authors_value]
    authors = []
    for author in authors_value:
        name = str(author.get("ce:indexed-name") or "").strip()
        if name:
            authors.append(name)
    return abstract, authors


def make_summary_config(config: dict[str, Any]) -> dict[str, Any]:
    return {"ai_summary": config["ai_summary"]}


def build_blocks(candidate: Candidate, summary: str, rank: int) -> list[dict[str, Any]]:
    authors = ", ".join(candidate.authors[:8]) + (", et al." if len(candidate.authors) > 8 else "")
    prefix = f"[Hot-{rank}]"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*{prefix} {escape_slack_mrkdwn(candidate.title)}*\n"
            f"{escape_slack_mrkdwn(authors or '著者情報なし')}\n"
            f"{escape_slack_mrkdwn(candidate.journal)} | {candidate.publication_date}"
        )}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*要約*\n{escape_slack_mrkdwn(summary)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*論文リンク*\n{candidate.url}"}},
        {"type": "divider"},
    ]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    elsevier_key = required_env("ELSEVIER_API_KEY")
    mendeley_client_id = required_env("MENDELEY_CLIENT_ID")
    mendeley_client_secret = required_env("MENDELEY_CLIENT_SECRET")
    openai_key = required_env("OPENAI_API_KEY")
    slack_token = required_env("SLACK_BOT_TOKEN")
    channel_id = required_env("SLACK_CHANNEL_ID")

    candidates = scopus_candidates(elsevier_key, config)
    logging.info("Scopus candidates after 96-hour scope filtering: %s", len(candidates))
    access_token = mendeley_access_token(mendeley_client_id, mendeley_client_secret)

    failures = 0
    for candidate in candidates:
        try:
            candidate.reader_count = mendeley_reader_count(access_token, candidate.doi)
        except (requests.RequestException, RuntimeError) as exc:
            failures += 1
            logging.warning("Mendeley lookup failed for DOI=%s: %s", candidate.doi, exc)
    if candidates and failures == len(candidates):
        raise RuntimeError("Every Mendeley Catalog lookup failed")

    ranked = rank_candidates(candidates, int(config["ranking"]["minimum_reader_count"]))
    logging.info(
        "Candidates with at least %s Mendeley reader(s): %s",
        config["ranking"]["minimum_reader_count"],
        len(ranked),
    )

    summarizer = OpenAISummarizer(openai_key, make_summary_config(config))
    slack = WebClient(token=slack_token)
    maximum_posts = int(config["posting"]["maximum_posts_per_day"])
    posted = 0

    for candidate in ranked:
        if posted >= maximum_posts:
            break
        candidate.abstract, authors = fetch_abstract(elsevier_key, candidate.doi)
        if authors:
            candidate.authors = authors
        if not candidate.abstract:
            logging.warning("Skipping DOI=%s because Scopus Abstract was unavailable", candidate.doi)
            continue

        paper = Paper(
            "",
            candidate.doi,
            candidate.title,
            candidate.authors,
            candidate.journal,
            candidate.publication_date,
            candidate.abstract,
            candidate.url,
        )
        try:
            summary = summarizer.summarize(
                paper.title,
                paper.abstract_original,
                paper.doi,
            ).text
        except SummaryError as exc:
            logging.warning("Skipping DOI=%s because summary failed: %s", candidate.doi, exc)
            continue

        rank = posted + 1
        prefix = f"[Hot-{rank}]"
        slack.chat_postMessage(
            channel=channel_id,
            text=f"{prefix} {candidate.title} — {summary}",
            blocks=build_blocks(candidate, summary, rank),
            unfurl_links=False,
            unfurl_media=False,
        )
        posted += 1
        logging.info(
            "Posted Hot-%s DOI=%s reader_count=%s",
            rank,
            candidate.doi,
            candidate.reader_count,
        )

    logging.info("Posted %s paper(s)", posted)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("Trending watch failed: %s", exc)
        sys.exit(1)
