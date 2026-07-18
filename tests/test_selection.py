from pathlib import Path

import yaml

from paper_watch.models import Paper
from paper_watch.selection import (
    eligible_candidates,
    is_excluded_source,
    select_for_run,
)


CONFIG = yaml.safe_load(
    (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
)


def make_paper(
    score: int,
    doi_suffix: str,
    journal: str = "Chemical Science",
    publication_date: str = "2026-07-17",
) -> Paper:
    return Paper(
        openalex_id=f"W{doi_suffix}",
        doi=f"10.0000/{doi_suffix}",
        title=f"Paper {score} {doi_suffix}",
        authors=["A. Author"],
        journal=journal,
        publication_date=publication_date,
        abstract_original="An abstract.",
        landing_page_url=f"https://doi.org/10.0000/{doi_suffix}",
        score=score,
        keyword_score=3,
    )


def test_only_score_15_or_above_is_eligible_and_ordered():
    eligible = eligible_candidates(
        [
            make_paper(20, "a"),
            make_paper(15, "b"),
            make_paper(14, "c"),
        ],
        CONFIG,
    )
    assert [paper.score for paper in eligible] == [20, 15]


def test_run_cap_keeps_only_top_five_papers():
    papers = [make_paper(30 - index, str(index)) for index in range(15)]
    eligible = eligible_candidates(papers, CONFIG)
    selected = select_for_run(
        eligible,
        config=CONFIG,
    )
    assert len(selected) == 5
    assert [paper.score for paper in selected] == list(range(30, 25, -1))


def test_equal_scores_prefer_newer_publication_date():
    eligible = eligible_candidates(
        [
            make_paper(20, "old", publication_date="2026-07-16"),
            make_paper(20, "new", publication_date="2026-07-19"),
        ],
        CONFIG,
    )
    assert [paper.doi for paper in eligible] == ["10.0000/new", "10.0000/old"]


def test_configured_sources_are_excluded_regardless_of_score():
    for index, journal in enumerate(
        [
            "JCIS Open",
            "Journal of Colloid and Interface Science Open",
            "figshare",
            "Research-Square",
            "ARXIV",
        ]
    ):
        paper = make_paper(99, str(index), journal)
        assert is_excluded_source(paper, CONFIG)
        assert eligible_candidates([paper], CONFIG) == []


def test_unlisted_source_is_not_excluded():
    paper = make_paper(20, "allowed", "Chemical Science")
    assert not is_excluded_source(paper, CONFIG)
    assert eligible_candidates([paper], CONFIG) == [paper]


def test_selection_never_exceeds_configured_run_cap():
    local_config = yaml.safe_load(yaml.safe_dump(CONFIG))
    local_config["posting"]["maximum_posts_per_run"] = 2
    eligible = eligible_candidates(
        [make_paper(20, "a"), make_paper(19, "b"), make_paper(18, "c")],
        local_config,
    )
    selected = select_for_run(
        eligible,
        config=local_config,
    )
    assert [paper.score for paper in selected] == [20, 19]
