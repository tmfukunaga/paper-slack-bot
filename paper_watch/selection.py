from __future__ import annotations

from .models import Paper
from .text_utils import normalize_doi, normalize_journal


def sort_key(paper: Paper) -> tuple[int, str, str]:
    """Sort papers by score, publication date, then title."""
    return paper.score, paper.publication_date, paper.title


def meets_posting_floor(paper: Paper, config: dict) -> bool:
    """Return whether a paper clears the single score and source filter."""
    return (
        paper.score >= int(config["posting"]["minimum_score"])
        and not is_excluded_source(paper, config)
    )


def is_excluded_source(paper: Paper, config: dict) -> bool:
    """Return whether the source name or DOI matches a configured exclusion."""
    source = normalize_journal(paper.journal)
    doi = normalize_doi(paper.doi)
    for item in config["excluded_sources"]:
        aliases = [item["canonical"], *item.get("aliases", [])]
        if source and any(source == normalize_journal(alias) for alias in aliases):
            return True
        prefixes = [normalize_doi(value) for value in item.get("doi_prefixes", [])]
        if doi and any(doi.startswith(prefix) for prefix in prefixes):
            return True
    return False


def eligible_candidates(papers: list[Paper], config: dict) -> list[Paper]:
    """Return score-qualified, non-excluded candidates in priority order."""
    accepted = [paper for paper in papers if meets_posting_floor(paper, config)]
    accepted.sort(key=sort_key, reverse=True)
    return accepted


def select_for_run(
    candidates: list[Paper],
    *,
    config: dict,
) -> list[Paper]:
    """Select papers before any OpenAI call.

    The result is capped per run. Only papers returned here may be summarized
    in this run.
    """
    run_cap = int(config["posting"]["maximum_posts_per_run"])
    return candidates[:run_cap]


def preview_selection(
    candidates: list[Paper],
    *,
    config: dict,
) -> list[Paper]:
    """Backward-compatible dry-run wrapper."""
    return select_for_run(candidates, config=config)
