from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml
from slack_sdk import WebClient

from paper_watch.ai_summary import OpenAISummarizer, SummaryError
from paper_watch.models import Paper
from paper_watch.text_utils import escape_slack_mrkdwn

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "trending_watch.yaml"
STATE_PATH = ROOT / "data" / "trending-watch-state.json"
SCOPUS_SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
SCOPUS_ABSTRACT_URL = "https://api.elsevier.com/content/abstract/doi/{doi}"
PLUMX_URL = "https://api.elsevier.com/analytics/plumx/doi/{doi}"


@dataclass
class Candidate:
    doi: str
    title: str
    authors: list[str]
    journal: str
    publication_date: str
    url: str
    abstract: str = ""
    metrics: dict[str, int] | None = None
    deltas: dict[str, int] | None = None
    score: float = 0.0


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else default


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def api_get(url: str, api_key: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
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


def scopus_candidates(api_key: str, config: dict[str, Any]) -> list[Candidate]:
    current_year = date.today().year
    query = f"SUBJAREA(CHEM) AND PUBYEAR > {current_year - 2}"
    params = {
        "query": query,
        # Scopus Search API accepts at most 25 STANDARD-view entries per page.
        "count": min(25, int(config["retrieval"]["maximum_candidates"])),
        "sort": "-coverDate",
        "view": "STANDARD",
    }
    payload = api_get(SCOPUS_SEARCH_URL, api_key, params=params)
    entries = payload.get("search-results", {}).get("entry", []) or []
    earliest = date.today() - timedelta(days=int(config["retrieval"]["publication_lookback_days"]))
    journals = {str(x).casefold() for x in config["scope"]["journals"]}
    keywords = [str(x).casefold() for x in config["scope"]["title_keywords"]]
    excluded = [str(x).casefold() for x in config["scope"]["excluded_title_keywords"]]
    results: list[Candidate] = []

    for entry in entries:
        doi = str(entry.get("prism:doi") or "").strip()
        title = str(entry.get("dc:title") or "").strip()
        journal = str(entry.get("prism:publicationName") or "").strip()
        published = str(entry.get("prism:coverDate") or "").strip()
        if not doi or not title or not published:
            continue
        try:
            if date.fromisoformat(published[:10]) < earliest:
                continue
        except ValueError:
            continue
        haystack = title.casefold()
        if any(term in haystack for term in excluded):
            continue
        if journal.casefold() not in journals and not any(term in haystack for term in keywords):
            continue
        author = str(entry.get("dc:creator") or "").strip()
        results.append(
            Candidate(
                doi=doi,
                title=title,
                authors=[author] if author else [],
                journal=journal,
                publication_date=published[:10],
                url=f"https://doi.org/{doi}",
            )
        )
    return results


def _walk_metrics(value: Any, category: str = "") -> dict[str, int]:
    totals = {"citations": 0, "usage": 0, "captures": 0, "mentions": 0, "socialMedia": 0}
    aliases = {
        "citation": "citations", "citations": "citations",
        "usage": "usage", "capture": "captures", "captures": "captures",
        "mention": "mentions", "mentions": "mentions",
        "socialmedia": "socialMedia", "social_media": "socialMedia",
    }
    if isinstance(value, list):
        for item in value:
            nested = _walk_metrics(item, category)
            for key in totals:
                totals[key] += nested[key]
        return totals
    if not isinstance(value, dict):
        return totals

    raw_category = str(value.get("category") or value.get("name") or category)
    normalized = re.sub(r"[^a-z_]", "", raw_category.casefold())
    active = aliases.get(normalized, category)
    count = value.get("count")
    if active in totals and isinstance(count, (int, float)):
        totals[active] += max(0, int(count))
    for key, nested_value in value.items():
        if key == "count":
            continue
        nested = _walk_metrics(nested_value, active)
        for metric in totals:
            totals[metric] += nested[metric]
    return totals


def plumx_metrics(api_key: str, doi: str) -> dict[str, int]:
    payload = api_get(PLUMX_URL.format(doi=quote(doi, safe="/")), api_key)
    return _walk_metrics(payload)


def fetch_abstract(api_key: str, doi: str) -> tuple[str, list[str]]:
    try:
        payload = api_get(SCOPUS_ABSTRACT_URL.format(doi=quote(doi, safe="/")), api_key)
    except (requests.RequestException, RuntimeError):
        return "", []
    core = payload.get("abstracts-retrieval-response", {}).get("coredata", {}) or {}
    abstract = str(core.get("dc:description") or "").strip()
    authors_value = payload.get("abstracts-retrieval-response", {}).get("authors", {}).get("author", []) or []
    if isinstance(authors_value, dict):
        authors_value = [authors_value]
    authors = []
    for author in authors_value:
        name = str(author.get("ce:indexed-name") or "").strip()
        if name:
            authors.append(name)
    return abstract, authors


def score_candidate(candidate: Candidate, previous: dict[str, Any], config: dict[str, Any]) -> None:
    current = candidate.metrics or {}
    prior = previous.get("snapshots", {}).get(candidate.doi.lower(), {}).get("metrics", {})
    candidate.deltas = {key: max(0, int(current.get(key, 0)) - int(prior.get(key, 0))) for key in current}
    has_baseline = bool(prior)
    source = candidate.deltas if has_baseline else current
    weights = config["ranking"]["weights"]
    raw = sum(math.log1p(source.get(key, 0)) * float(weights.get(key, 0)) for key in weights)
    try:
        age = max(0, (date.today() - date.fromisoformat(candidate.publication_date)).days)
    except ValueError:
        age = 30
    candidate.score = raw / (1.0 + age * float(config["ranking"]["daily_age_penalty"]))


def make_summary_config(config: dict[str, Any]) -> dict[str, Any]:
    return {"ai_summary": config["ai_summary"]}


def build_blocks(candidate: Candidate, summary: str) -> list[dict[str, Any]]:
    authors = ", ".join(candidate.authors[:8]) + (", et al." if len(candidate.authors) > 8 else "")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*［話題］{escape_slack_mrkdwn(candidate.title)}*\n"
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
    openai_key = required_env("OPENAI_API_KEY")
    slack_token = required_env("SLACK_BOT_TOKEN")
    channel_id = required_env("SLACK_CHANNEL_ID")
    state = load_json(STATE_PATH, {"snapshots": {}, "posted": {}})
    candidates = scopus_candidates(elsevier_key, config)
    logging.info("Scopus candidates after scope filtering: %s", len(candidates))

    usable: list[Candidate] = []
    for candidate in candidates:
        if candidate.doi.lower() in state.get("posted", {}):
            continue
        try:
            candidate.metrics = plumx_metrics(elsevier_key, candidate.doi)
        except requests.HTTPError as exc:
            logging.warning("PlumX unavailable for DOI=%s: %s", candidate.doi, exc)
            continue
        score_candidate(candidate, state, config)
        if candidate.score > 0:
            usable.append(candidate)

    usable.sort(key=lambda item: (item.score, item.publication_date), reverse=True)
    selected = usable[: int(config["posting"]["maximum_posts_per_day"])]
    summarizer = OpenAISummarizer(openai_key, make_summary_config(config))
    slack = WebClient(token=slack_token)
    posted_now: set[str] = set()

    for candidate in selected:
        candidate.abstract, authors = fetch_abstract(elsevier_key, candidate.doi)
        if authors:
            candidate.authors = authors
        if not candidate.abstract:
            logging.warning("Skipping DOI=%s because Scopus Abstract was unavailable", candidate.doi)
            continue
        paper = Paper("", candidate.doi, candidate.title, candidate.authors, candidate.journal,
                      candidate.publication_date, candidate.abstract, candidate.url)
        try:
            summary = summarizer.summarize(paper.title, paper.abstract_original, paper.doi).text
        except SummaryError as exc:
            logging.warning("Skipping DOI=%s because summary failed: %s", candidate.doi, exc)
            continue
        response = slack.chat_postMessage(
            channel=channel_id,
            text=f"［話題］{candidate.title} — {summary}",
            blocks=build_blocks(candidate, summary),
            unfurl_links=False,
            unfurl_media=False,
        )
        state.setdefault("posted", {})[candidate.doi.lower()] = {
            "posted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "slack_ts": str(response.get("ts") or ""),
            "title": candidate.title,
        }
        posted_now.add(candidate.doi.lower())

    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for candidate in candidates:
        if candidate.metrics is not None:
            state.setdefault("snapshots", {})[candidate.doi.lower()] = {
                "observed_at": observed_at,
                "metrics": candidate.metrics,
                "title": candidate.title,
            }
    save_json(STATE_PATH, state)
    logging.info("Posted %s paper(s)", len(posted_now))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("Trending watch failed: %s", exc)
        sys.exit(1)
