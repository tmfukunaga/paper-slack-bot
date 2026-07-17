from __future__ import annotations

import logging
import os
import time
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
        abstract_original=reconstruct_abstract(
            work.get("abstract_inverted_index")
        ),
        landing_page_url=landing_page_url,
    )


def _get_with_retry(
    params: dict[str, Any],
    max_attempts: int = 6,
) -> requests.Response:
    contact_email = os.environ.get("CONTACT_EMAIL", "")

    headers = {
        "User-Agent": (
            f"paper-slack-bot/1.0 (mailto:{contact_email})"
            if contact_email
            else "paper-slack-bot/1.0"
        )
    }

    waits = [15, 30, 60, 120, 240, 300]
    last_response: requests.Response | None = None

    for attempt in range(max_attempts):
        response = requests.get(
            BASE_URL,
            params=params,
            headers=headers,
            timeout=60,
        )

        last_response = response

        if response.status_code == 200:
            return response

        error_text = response.text[:500].replace("\n", " ")

        LOGGER.warning(
            "OpenAlex returned status=%s, remaining=%s, reset=%s, body=%s",
            response.status_code,
            response.headers.get("X-RateLimit-Remaining"),
            response.headers.get("X-RateLimit-Reset"),
            error_text,
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")

            if retry_after and retry_after.isdigit():
                wait_seconds = int(retry_after)
            else:
                wait_seconds = waits[attempt]

            LOGGER.warning(
                "OpenAlex 429. Waiting %s seconds before retry %s/%s.",
                wait_seconds,
                attempt + 1,
                max_attempts,
            )

            time.sleep(wait_seconds)
            continue

        if 500 <= response.status_code < 600:
            wait_seconds = waits[attempt]

            LOGGER.warning(
                "OpenAlex server error. Waiting %s seconds before retry.",
                wait_seconds,
            )

            time.sleep(wait_seconds)
            continue

        response.raise_for_status()

    if last_response is not None:
        last_response.raise_for_status()

    raise RuntimeError("OpenAlex request failed without a response.")


def fetch_candidates(
    api_key: str,
    config: dict[str, Any],
) -> list[Paper]:
    runtime = config["runtime"]

    today = date.today()

    created_from = today - timedelta(
        days=int(runtime["lookback_created_days"])
    )

    published_from = today - timedelta(
        days=int(runtime["lookback_publication_days"])
    )

    queries = [
        _boolean_query(config["keywords"]["core"]["terms"]),
        _boolean_query(config["keywords"]["strong"]["terms"]),
    ]

    papers: dict[str, Paper] = {}

    for query in queries:
        for page in range(
            1,
            int(runtime["pages_per_query"]) + 1,
        ):
            params = {
                "api_key": api_key,
                "search": query,
                "filter": (
                    f"from_created_date:{created_from.isoformat()},"
                    f"from_publication_date:{published_from.isoformat()},"
                    "has_doi:true,type:article|review"
                ),
                "sort": "publication_date:desc",
                "per_page": min(
                    int(runtime["results_per_page"]),
                    100,
                ),
                "page": page,
            }

            response = _get_with_retry(params)
            payload = response.json()

            results = payload.get("results", [])

            LOGGER.info(
                "OpenAlex query page=%s results=%s cost_usd=%s",
                page,
                len(results),
                payload.get("meta", {}).get("cost_usd"),
            )

            for work in results:
                paper = _paper_from_work(work)

                if paper and paper.title:
                    papers[paper.key] = paper

            if len(results) < int(runtime["results_per_page"]):
                break

            time.sleep(1)

    return list(papers.values())
