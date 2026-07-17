from pathlib import Path

import yaml

from paper_watch.models import Paper
from paper_watch.scoring import score_paper


CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))


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


def test_core_title_forces_high_score_without_double_counting_nanobelt():
    result = score_paper(
        paper("Synthesis of a carbon nanobelt", "A macrocyclic compound.", "Chemistry Letters"),
        CONFIG,
    )
    assert "carbon nanobelt" in result.core_title
    assert "nanobelt" not in result.core_title
    assert result.score >= 14
    assert result.force_post


def test_nature_communications_is_tier_s():
    result = score_paper(
        paper("A supramolecular macrocycle", "Host-guest chemistry.", "Nature Communications"),
        CONFIG,
    )
    assert result.journal_tier == "S"
    assert result.journal_score == 10


def test_exclusion_penalty():
    result = score_paper(
        paper("A macrocyclic peptide for cancer", "A protein-based biomedical system.", "Chemical Science"),
        CONFIG,
    )
    assert result.score < 0


def test_plural_forms_and_cpps_match_with_chemistry_context():
    result = score_paper(
        paper("Cycloparaphenylenes and CPPs as molecular nanocarbons", "Macrocycles are studied.", "Organic Letters"),
        CONFIG,
    )
    assert "cycloparaphenylene" in result.core_title
    assert "CPP" in result.core_title
    assert "macrocycle" in result.strong_abstract


def test_cpp_without_chemistry_context_is_ignored():
    result = score_paper(
        paper("A faster CPP compiler", "A systems programming benchmark.", "Science"),
        CONFIG,
    )
    assert "CPP" not in result.core_title
