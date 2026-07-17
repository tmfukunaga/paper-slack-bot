from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import requests

from .models import Paper
from .text_utils import doi_url, normalize_doi, reconstruct_abstract

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://api.openalex.org/works"


def _boolean_query(terms: list[str]) -> str:
    operands = []
    for term in terms:
        escaped = term.replace('"', '\\"')
        operands.append(f'"{escaped}"')
    return "(" + " OR ".join(operands) + ")"


def _paper_from_work(work: dict[str, Any]) -> Paper | None:
    doi = normalize_doi(work.get("doi") or "")
    if not doi:
        return None
    authors = [
        authorship.get("author", {}).get("display_name", "").strip()
        for authorship in work.get("authorships", [])
        if authorship.get("author", {}).get("display_name")
    ]
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    landing_page_url = location.get("landing_page_url") or doi_url(doi)
    return Paper(
        openalex_id=work.get("id", ""),
        doi=doi,
        title=(work.get("title") or work.get("display_name") or "").strip(),
        authors=authors,
        journal=(source.get("display_name") or "").strip(),
        publication_date=work.get("publication_date") or "",
        abstract_original=reconstruct_abstract(work.get("abstract_inverted_index")),
        landing_page_url=landing_page_url,
    )


def fetch_candidates(api_key: str, config: dict[str, Any]) -> list[Paper]:
    runtime = config["runtime"]
    today = date.today()
    created_from = today - timedelta(days=int(runtime["lookback_created_days"]))
    published_from = today - timedelta(days=int(runtime["lookback_publication_days"]))
    queries = [
        _boolean_query(config["keywords"]["core"]["terms"]),
        _boolean_query(config["keywords"]["strong"]["terms"]),
    ]

    papers: dict[str, Paper] = {}
    for query in queries:
        for page in range(1, int(runtime["pages_per_query"]) + 1):
            params = {
                "api_key": api_key,
                "search": query,
                "filter": (
                    f"from_created_date:{created_from.isoformat()},"
                    f"from_publication_date:{published_from.isoformat()},"
                    "has_doi:true,type:article|review"
                ),
                "sort": "-publication_date",
                "per_page": min(int(runtime["results_per_page"]), 100),
                "page": page,
            }
            response = requests.get(BASE_URL, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()
            LOGGER.info(
                "OpenAlex query page=%s results=%s cost_usd=%s",
                page,
                len(payload.get("results", [])),
                payload.get("meta", {}).get("cost_usd"),
            )
            for work in payload.get("results", []):
                paper = _paper_from_work(work)
                if paper and paper.title:
                    papers[paper.key] = paper
            if len(payload.get("results", [])) < int(runtime["results_per_page"]):
                break
    return list(papers.values())
