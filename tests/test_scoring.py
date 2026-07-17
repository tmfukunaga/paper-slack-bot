from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
import yaml

from paper_watch.article_image import ArticleImageFetcher, extract_html_candidates
from paper_watch.config_validation import ConfigError, validate_config
from paper_watch.models import Paper
from paper_watch.openalex_client import build_work_filter
from paper_watch.scoring import apply_score, score_paper
from paper_watch.slack_client import build_blocks


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


def test_config_is_valid():
    validate_config(CONFIG)


def test_duplicate_keyword_is_rejected():
    broken = deepcopy(CONFIG)
    broken["keywords"]["strong"]["terms"].append("nanobelt")
    with pytest.raises(ConfigError, match="duplicated"):
        validate_config(broken)


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


def test_chemrxiv_is_tier_a():
    result = score_paper(
        paper(
            "A molecular nanocarbon",
            "A macrocyclic structure.",
            "ChemRxiv",
        ),
        CONFIG,
    )
    assert result.journal_tier == "A"
    assert result.journal_score == 7


def test_exclusion_penalty_filters_peptide_cpp_by_score():
    result = score_paper(
        paper(
            "A CPP framework for cell-penetrating peptides",
            "Cell-penetrating peptides are protein-derived systems.",
            "Protein Science",
        ),
        CONFIG,
    )
    assert "CPP" in result.core_title
    assert "peptide" in result.exclude_title
    assert result.score < int(CONFIG["posting"]["minimum_total_score"])


def test_tags_contain_only_positive_matches():
    item = paper(
        "A macrocyclic peptide",
        "A supramolecular protein system.",
        "Chemical Science",
    )
    result = score_paper(item, CONFIG)
    apply_score(item, result, CONFIG)
    assert "#macrocyclic" in item.tags
    assert "#supramolecular" in item.tags
    assert "#peptide" not in item.tags


def test_free_openalex_filter_includes_preprints_and_no_created_date():
    value = build_work_filter(date(2026, 7, 1))
    assert "from_publication_date:2026-07-01" in value
    assert "type:article|review|preprint" in value
    assert "from_created_date" not in value


def test_message_order_and_only_final_divider():
    item = paper(
        "A molecular nanocarbon",
        "This is the abstract.",
        "ChemRxiv",
    )
    result = score_paper(item, CONFIG)
    apply_score(item, result, CONFIG)
    blocks = build_blocks(item, slack_file_id="F123")

    assert blocks[0]["type"] == "section"
    assert "Matched keywords" in blocks[1]["text"]["text"]
    assert blocks[2]["type"] == "image"
    assert blocks[3]["type"] == "actions"
    assert blocks[4]["text"]["text"] == "*Abstract*"
    divider_indexes = [
        index for index, block in enumerate(blocks) if block["type"] == "divider"
    ]
    assert divider_indexes == [len(blocks) - 1]


def test_html_candidate_prefers_open_graph_image():
    html = """
    <html><head>
      <meta property="og:image" content="/social/article.jpg">
    </head><body>
      <img src="/assets/logo.png" width="800" height="400" alt="Publisher logo">
      <img src="/figures/figure1.jpg" width="1000" height="600" alt="Figure 1">
    </body></html>
    """
    candidates = extract_html_candidates(html, "https://example.org/paper")
    assert candidates[0].url == "https://example.org/social/article.jpg"


def test_fallback_card_is_generated_when_requested():
    with ArticleImageFetcher(CONFIG) as fetcher:
        result = fetcher.fetch(
            "https://127.0.0.1.invalid/paper",
            "10.0000/test",
            "10.0000/test",
            title="A molecular nanocarbon with a very long title for fallback image generation",
            journal="ChemRxiv",
            publication_date="2026-07-17",
        )
        assert result is not None
        assert result.path.is_file()
        assert result.method == "generated fallback card"


def test_message_rejects_missing_image_file_id():
    item = paper(
        "A molecular nanocarbon",
        "This is the abstract.",
        "ChemRxiv",
    )
    result = score_paper(item, CONFIG)
    apply_score(item, result, CONFIG)
    with pytest.raises(ValueError, match="every paper must have an image"):
        build_blocks(item, slack_file_id="")
