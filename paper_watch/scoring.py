from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import Paper
from .text_utils import normalize_journal, normalize_text


@dataclass
class ScoreResult:
    score: int
    journal_tier: str
    journal_score: int
    keyword_score: int
    exclusion_penalty: int
    core_title: list[str]
    core_abstract: list[str]
    strong_title: list[str]
    strong_abstract: list[str]
    weak_title: list[str]
    weak_abstract: list[str]
    exclude_title: list[str]
    exclude_abstract: list[str]

    @property
    def has_content_match(self) -> bool:
        return bool(
            self.core_title
            or self.core_abstract
            or self.strong_title
            or self.strong_abstract
            or self.weak_title
            or self.weak_abstract
        )


def _term_pattern(term: str) -> re.Pattern[str]:
    normalized = normalize_text(term).lower()
    escaped = re.escape(normalized).replace(r"\ ", r"\s+")

    if (
        normalized.endswith("y")
        and len(normalized) >= 2
        and normalized[-2] not in "aeiou"
    ):
        escaped = escaped[:-1] + r"(?:y|ies)"
    elif not normalized.endswith("s"):
        escaped += r"s?"

    return re.compile(
        rf"(?<![a-z0-9]){escaped}(?![a-z0-9])",
        re.IGNORECASE,
    )


def _non_overlapping_matches(text: str, terms: list[str]) -> list[str]:
    normalized = normalize_text(text).lower()
    accepted_spans: list[tuple[int, int]] = []
    matched: list[str] = []

    for term in sorted(
        terms,
        key=lambda value: len(normalize_text(value)),
        reverse=True,
    ):
        pattern = _term_pattern(term)

        for match in pattern.finditer(normalized):
            span = match.span()
            overlaps = any(
                not (span[1] <= old[0] or span[0] >= old[1])
                for old in accepted_spans
            )
            if overlaps:
                continue
            accepted_spans.append(span)
            matched.append(term)
            break

    return matched


def journal_score(journal: str, config: dict[str, Any]) -> tuple[str, int]:
    target = normalize_journal(journal)

    for tier_name, tier in config["journal_tiers"].items():
        for item in tier["journals"]:
            aliases = item.get("aliases", []) + [item.get("canonical", "")]
            if any(
                target == normalize_journal(alias)
                for alias in aliases
                if alias
            ):
                return (
                    tier_name.replace("tier_", "").upper(),
                    int(tier["score"]),
                )

    return "unlisted", 0


def score_paper(paper: Paper, config: dict[str, Any]) -> ScoreResult:
    core = config["keywords"]["core"]
    strong = config["keywords"]["strong"]
    weak = config["keywords"]["weak"]
    exclude = config["exclusion_keywords"]

    core_title = _non_overlapping_matches(paper.title, core["terms"])
    core_abstract = _non_overlapping_matches(
        paper.abstract_original,
        core["terms"],
    )
    strong_title = _non_overlapping_matches(paper.title, strong["terms"])
    strong_abstract = _non_overlapping_matches(
        paper.abstract_original,
        strong["terms"],
    )
    weak_title = _non_overlapping_matches(paper.title, weak["terms"])
    weak_abstract = _non_overlapping_matches(
        paper.abstract_original,
        weak["terms"],
    )
    exclude_title = _non_overlapping_matches(
        paper.title,
        exclude["terms"],
    )
    exclude_abstract = _non_overlapping_matches(
        paper.abstract_original,
        exclude["terms"],
    )

    tier, j_score = journal_score(paper.journal, config)

    keyword_score = 0
    keyword_score += min(
        len(core_title) * int(core["title_score"]),
        int(core["title_cap"]),
    )
    keyword_score += min(
        len(core_abstract) * int(core["abstract_score"]),
        int(core["abstract_cap"]),
    )
    keyword_score += min(
        len(strong_title) * int(strong["title_score"]),
        int(strong["title_cap"]),
    )
    keyword_score += min(
        len(strong_abstract) * int(strong["abstract_score"]),
        int(strong["abstract_cap"]),
    )
    keyword_score += min(
        len(weak_title) * int(weak["title_score"]),
        int(weak["title_cap"]),
    )
    keyword_score += min(
        len(weak_abstract) * int(weak["abstract_score"]),
        int(weak["abstract_cap"]),
    )

    exclusion_penalty = 0
    exclusion_penalty += min(
        len(exclude_title) * int(exclude["title_penalty"]),
        int(exclude["title_cap"]),
    )
    exclusion_penalty += min(
        len(exclude_abstract) * int(exclude["abstract_penalty"]),
        int(exclude["abstract_cap"]),
    )

    score = j_score + keyword_score - exclusion_penalty

    return ScoreResult(
        score=score,
        journal_tier=tier,
        journal_score=j_score,
        keyword_score=keyword_score,
        exclusion_penalty=exclusion_penalty,
        core_title=core_title,
        core_abstract=core_abstract,
        strong_title=strong_title,
        strong_abstract=strong_abstract,
        weak_title=weak_title,
        weak_abstract=weak_abstract,
        exclude_title=exclude_title,
        exclude_abstract=exclude_abstract,
    )


def _slug(term: str) -> str:
    value = normalize_text(term).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def apply_score(
    paper: Paper,
    result: ScoreResult,
    config: dict[str, Any],
) -> None:
    paper.score = result.score
    paper.keyword_score = result.keyword_score
    paper.journal_score = result.journal_score
    paper.exclusion_penalty = result.exclusion_penalty
    paper.journal_tier = result.journal_tier
    paper.matched_core_title = result.core_title
    paper.matched_core_abstract = result.core_abstract
    paper.matched_strong_title = result.strong_title
    paper.matched_strong_abstract = result.strong_abstract
    paper.matched_weak_title = result.weak_title
    paper.matched_weak_abstract = result.weak_abstract
    paper.matched_exclude_title = result.exclude_title
    paper.matched_exclude_abstract = result.exclude_abstract

    positive = (
        result.core_title
        + result.core_abstract
        + result.strong_title
        + result.strong_abstract
        + result.weak_title
        + result.weak_abstract
    )
    unique_positive = list(dict.fromkeys(positive))
    maximum = int(config["posting"]["maximum_tags_displayed"])
    paper.tags = [
        f"#{_slug(term)}"
        for term in unique_positive[:maximum]
        if _slug(term)
    ]
