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
    """High-score papers are posted regardless of the soft volume targets."""
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
    )


def preview_selection(
    guaranteed: list[Paper],
    conditional: list[Paper],
    *,
    successful_before_run_today: int,
    config: dict,
) -> list[Paper]:
    """Predict selection for dry-run assuming every attempted post succeeds."""
    selected = list(guaranteed)
    successful_this_run = len(guaranteed)

    for paper in conditional:
        if not can_post_conditional(
            successful_this_run=successful_this_run,
            successful_before_run_today=successful_before_run_today,
            config=config,
        ):
            break
        selected.append(paper)
        successful_this_run += 1

    return selected
