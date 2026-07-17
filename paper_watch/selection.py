from __future__ import annotations

from .models import Paper


def sort_key(paper: Paper) -> tuple[int, str, str]:
    """Sort papers by score, publication date, then title."""
    return paper.score, paper.publication_date, paper.title


def meets_posting_floor(paper: Paper, config: dict) -> bool:
    """Return whether a paper is eligible for either posting class."""
    posting = config["posting"]
    return (
        paper.score >= int(posting["conditional_minimum_score"])
        and paper.keyword_score >= int(posting["minimum_keyword_score"])
    )


def is_guaranteed(paper: Paper, config: dict) -> bool:
    """Return whether a paper belongs to the high-score class."""
    return paper.score >= int(config["posting"]["guaranteed_score"])


def split_candidates(
    papers: list[Paper],
    config: dict,
) -> tuple[list[Paper], list[Paper]]:
    """Return high-score and conditional candidates, each score-sorted."""
    accepted = [paper for paper in papers if meets_posting_floor(paper, config)]
    accepted.sort(key=sort_key, reverse=True)

    guaranteed = [paper for paper in accepted if is_guaranteed(paper, config)]
    conditional = [paper for paper in accepted if not is_guaranteed(paper, config)]
    return guaranteed, conditional


def can_post_conditional(
    *,
    successful_this_run: int,
    successful_before_run_today: int,
    config: dict,
) -> bool:
    """Return whether another score-11-to-14 paper may fill a soft slot."""
    posting = config["posting"]
    return (
        successful_this_run < int(posting["target_posts_per_run"])
        and successful_before_run_today + successful_this_run
        < int(posting["target_posts_per_day"])
        and successful_this_run < int(posting["maximum_posts_per_run"])
    )


def select_for_run(
    guaranteed: list[Paper],
    conditional: list[Paper],
    *,
    successful_before_run_today: int,
    config: dict,
) -> list[Paper]:
    """Select papers before any OpenAI call.

    Rules:
    - Score >= guaranteed_score has priority, but the whole run is hard-capped.
    - Score 11-14 only fills the soft run target while the daily soft target remains.
    - Only papers returned here may be summarized in this run.
    """
    posting = config["posting"]
    hard_cap = int(posting["maximum_posts_per_run"])

    selected = list(guaranteed[:hard_cap])
    selected_keys = {paper.key for paper in selected}

    successful_this_run = len(selected)
    for paper in conditional:
        if successful_this_run >= hard_cap:
            break
        if not can_post_conditional(
            successful_this_run=successful_this_run,
            successful_before_run_today=successful_before_run_today,
            config=config,
        ):
            break
        if paper.key in selected_keys:
            continue
        selected.append(paper)
        selected_keys.add(paper.key)
        successful_this_run += 1

    selected.sort(key=sort_key, reverse=True)
    return selected


def preview_selection(
    guaranteed: list[Paper],
    conditional: list[Paper],
    *,
    successful_before_run_today: int,
    config: dict,
) -> list[Paper]:
    """Backward-compatible dry-run wrapper."""
    return select_for_run(
        guaranteed,
        conditional,
        successful_before_run_today=successful_before_run_today,
        config=config,
    )
