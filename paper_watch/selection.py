from __future__ import annotations

from .models import Paper
from .text_utils import normalize_journal


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
    """Return whether the OpenAlex source matches a configured exclusion."""
    source = normalize_journal(paper.journal)
    if not source:
        return False
    for item in config["excluded_sources"]:
        aliases = [item["canonical"], *item.get("aliases", [])]
        if any(source == normalize_journal(alias) for alias in aliases):
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
    successful_before_run_today: int,
    config: dict,
) -> list[Paper]:
    """Select papers before any OpenAI call.

    The result is capped both per run and by the remaining daily allowance.
    Only papers returned here may be summarized in this run.
    """
    posting = config["posting"]
    run_cap = int(posting["maximum_posts_per_run"])
    daily_cap = int(posting["maximum_posts_per_day"])
    remaining_today = max(0, daily_cap - successful_before_run_today)
    return candidates[: min(run_cap, remaining_today)]


def preview_selection(
    candidates: list[Paper],
    *,
    successful_before_run_today: int,
    config: dict,
) -> list[Paper]:
    """Backward-compatible dry-run wrapper."""
    return select_for_run(
        candidates,
        successful_before_run_today=successful_before_run_today,
        config=config,
    )
