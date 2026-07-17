from pathlib import Path

import yaml

from paper_watch.models import Paper
from paper_watch.selection import (
    can_post_conditional,
    select_for_run,
    split_candidates,
)


CONFIG = yaml.safe_load(
    (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
)


def make_paper(score: int, doi_suffix: str) -> Paper:
    return Paper(
        openalex_id=f"W{doi_suffix}",
        doi=f"10.0000/{doi_suffix}",
        title=f"Paper {score} {doi_suffix}",
        authors=["A. Author"],
        journal="Chemical Science",
        publication_date="2026-07-17",
        abstract_original="An abstract.",
        landing_page_url=f"https://doi.org/10.0000/{doi_suffix}",
        score=score,
        keyword_score=3,
    )


def test_high_scores_are_all_guaranteed_before_hard_cap():
    guaranteed, conditional = split_candidates(
        [make_paper(24, "a"), make_paper(18, "b"), make_paper(15, "c")],
        CONFIG,
    )
    assert [paper.score for paper in guaranteed] == [24, 18, 15]
    assert conditional == []


def test_conditional_scores_fill_three_slots_in_score_order():
    guaranteed, conditional = split_candidates(
        [
            make_paper(10, "x"),
            make_paper(11, "a"),
            make_paper(14, "b"),
            make_paper(13, "c"),
            make_paper(12, "d"),
        ],
        CONFIG,
    )
    selected = select_for_run(
        guaranteed,
        conditional,
        successful_before_run_today=0,
        config=CONFIG,
    )
    assert [paper.score for paper in selected] == [14, 13, 12]


def test_guaranteed_papers_consume_soft_slots_before_conditional_fill():
    guaranteed, conditional = split_candidates(
        [
            make_paper(20, "a"),
            make_paper(16, "b"),
            make_paper(14, "c"),
            make_paper(13, "d"),
        ],
        CONFIG,
    )
    selected = select_for_run(
        guaranteed,
        conditional,
        successful_before_run_today=0,
        config=CONFIG,
    )
    assert [paper.score for paper in selected] == [20, 16, 14]


def test_hard_cap_keeps_only_top_ten_guaranteed_papers():
    papers = [make_paper(30 - index, str(index)) for index in range(15)]
    guaranteed, conditional = split_candidates(papers, CONFIG)
    selected = select_for_run(
        guaranteed,
        conditional,
        successful_before_run_today=0,
        config=CONFIG,
    )
    assert len(selected) == 10
    assert [paper.score for paper in selected] == list(range(30, 20, -1))


def test_guaranteed_papers_are_not_limited_by_soft_daily_target():
    guaranteed, conditional = split_candidates(
        [
            make_paper(22, "a"),
            make_paper(20, "b"),
            make_paper(18, "c"),
            make_paper(15, "d"),
            make_paper(14, "e"),
        ],
        CONFIG,
    )
    selected = select_for_run(
        guaranteed,
        conditional,
        successful_before_run_today=30,
        config=CONFIG,
    )
    assert [paper.score for paper in selected] == [22, 20, 18, 15]


def test_daily_soft_target_limits_only_conditional_papers():
    assert can_post_conditional(
        successful_this_run=0,
        successful_before_run_today=29,
        config=CONFIG,
    )
    assert not can_post_conditional(
        successful_this_run=1,
        successful_before_run_today=29,
        config=CONFIG,
    )
    assert not can_post_conditional(
        successful_this_run=0,
        successful_before_run_today=30,
        config=CONFIG,
    )


def test_selection_never_exceeds_configured_hard_cap():
    local_config = yaml.safe_load(yaml.safe_dump(CONFIG))
    local_config["posting"]["maximum_posts_per_run"] = 2
    local_config["posting"]["target_posts_per_run"] = 2
    guaranteed, conditional = split_candidates(
        [make_paper(20, "a"), make_paper(19, "b"), make_paper(18, "c")],
        local_config,
    )
    selected = select_for_run(
        guaranteed,
        conditional,
        successful_before_run_today=0,
        config=local_config,
    )
    assert [paper.score for paper in selected] == [20, 19]
