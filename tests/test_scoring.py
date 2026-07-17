from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
import yaml

from paper_watch.ai_summary import (
    build_summary_instructions,
    clean_summary,
    extract_output_text,
    summary_character_count,
)
from paper_watch.config_validation import ConfigError, validate_config
from paper_watch.models import Paper
from paper_watch.openalex_client import build_work_filter
from paper_watch.scoring import apply_score, score_paper
from paper_watch.slack_client import build_paper_blocks


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


def test_invalid_summary_character_range_is_rejected():
    broken = deepcopy(CONFIG)
    broken["ai_summary"]["minimum_characters"] = 130
    with pytest.raises(ConfigError, match="character settings"):
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
    assert result.score < int(CONFIG["posting"]["conditional_minimum_score"])


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


def test_summary_prompt_keeps_chemical_names_in_english():
    instructions = build_summary_instructions(CONFIG)
    assert "化合物名" in instructions
    assert "英語表記のままで構いません" in instructions
    assert "推測" in instructions


def test_response_output_text_extraction():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "日本語の要約です。"}
                ],
            }
        ]
    }
    assert extract_output_text(payload) == "日本語の要約です。"


def test_summary_cleanup_and_count():
    value = clean_summary("```text\n要約： **aza[6]helicene**を合成した。\n```")
    assert value == "aza[6]heliceneを合成した。"
    assert summary_character_count(value) == len(value)


def test_slack_message_contains_summary_not_english_abstract():
    item = paper(
        "A molecular nanocarbon",
        "This English abstract must not be displayed.",
        "ChemRxiv",
    )
    result = score_paper(item, CONFIG)
    apply_score(item, result, CONFIG)
    item.summary_japanese = "macrocycleの新規合成法を開発し、構造解析により高い円筒性と自己集合挙動を明らかにした。"
    item.summary_language = "ja"

    blocks = build_paper_blocks(item)
    rendered = "\n".join(
        block.get("text", {}).get("text", "")
        for block in blocks
        if isinstance(block, dict)
    )
    assert "Matched keywords" in rendered
    assert "*要約*" in rendered
    assert item.summary_japanese in rendered
    assert item.abstract_original not in rendered
    assert blocks[-1]["type"] == "divider"
    assert sum(block["type"] == "divider" for block in blocks) == 1
