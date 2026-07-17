from pathlib import Path

import yaml

from paper_watch.models import Paper
from paper_watch.scoring import apply_score, score_paper


CONFIG = yaml.safe_load(
    (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
)


def paper(title: str, abstract: str, journal: str) -> Paper:
    return Paper(
        openalex_id="W1",
        doi="10.0000/test",
        title=title,
        authors=["A. Author"],
        journal=journal,
        publication_date="2026-07-17",
        abstract_original=abstract,
        landing_page_url="https://doi.org/10.0000/test",
    )


def test_nanobelt_does_not_double_count_nested_keyword():
    result = score_paper(
        paper(
            "Synthesis of a carbon nanobelt",
            "A macrocyclic compound.",
            "Chemistry Letters",
        ),
        CONFIG,
    )
    assert "carbon nanobelt" in result.core_title
    assert "nanobelt" not in result.core_title


def test_nature_communications_is_tier_s():
    result = score_paper(
        paper(
            "A supramolecular macrocycle",
            "Host-guest chemistry.",
            "Nature Communications",
        ),
        CONFIG,
    )
    assert result.journal_tier == "S"
    assert result.journal_score == 10


def test_exclusion_penalty_makes_peptide_cpp_negative():
    result = score_paper(
        paper(
            "A CPP framework for cell-penetrating peptides",
            "Cell-penetrating peptides (CPPs) are protein-derived systems.",
            "Protein Science",
        ),
        CONFIG,
    )
    assert "CPP" in result.core_title
    assert "peptide" in result.exclude_title
    assert result.score < int(CONFIG["runtime"]["post_threshold"])


def test_cpp_is_not_special_cased():
    result = score_paper(
        paper(
            "A CPP molecular framework",
            "A molecular system.",
            "Organic Letters",
        ),
        CONFIG,
    )
    assert "CPP" in result.core_title


def test_tags_contain_only_positive_matches():
    p = paper(
        "A macrocyclic peptide",
        "A supramolecular protein system.",
        "Chemical Science",
    )
    result = score_paper(p, CONFIG)
    apply_score(p, result)
    assert "#macrocyclic" in p.tags
    assert "#supramolecular" in p.tags
    assert "#peptide" not in p.tags
