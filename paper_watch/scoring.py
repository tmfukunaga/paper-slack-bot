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
    exclude_title: list[str]
    exclude_abstract: list[str]

    @property
    def has_content_match(self) -> bool:
        return bool(self.core_title or self.core_abstract or self.strong_title or self.strong_abstract)

    @property
    def force_post(self) -> bool:
        return bool(self.core_title)


def _term_pattern(term: str) -> re.Pattern[str]:
    if term == "CPP":
        return re.compile(r"(?<![A-Za-z0-9])CPPs?(?![A-Za-z0-9])")
    normalized = normalize_text(term).lower()
    escaped = re.escape(normalized).replace(r"\ ", r"\s+")
    if normalized.endswith("y") and len(normalized) >= 2 and normalized[-2] not in "aeiou":
        escaped = escaped[:-1] + r"(?:y|ies)"
    elif not normalized.endswith("s"):
        escaped += r"s?"
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


def _non_overlapping_matches(text: str, terms: list[str]) -> list[str]:
    normalized = normalize_text(text)
    lowered = normalized.lower()
    accepted_spans: list[tuple[int, int]] = []
    matched: list[str] = []

    for term in sorted(terms, key=lambda x: len(normalize_text(x)), reverse=True):
        source = normalized if term == "CPP" else lowered
        pattern = _term_pattern(term)
        for match in pattern.finditer(source):
            span = match.span()
            overlaps = any(not (span[1] <= old[0] or span[0] >= old[1]) for old in accepted_spans)
            if overlaps:
                continue
            accepted_spans.append(span)
            matched.append(term)
            break
    return matched



def _remove_ambiguous_without_context(
    title: str, abstract: str, matches: list[str], config: dict[str, Any]
) -> list[str]:
    ambiguous = config.get("keywords", {}).get("ambiguous", {})
    ambiguous_terms = set(ambiguous.get("terms", []))
    if not ambiguous_terms.intersection(matches):
        return matches
    combined = f"{title}\n{abstract}"
    context_terms = ambiguous.get("required_context", [])
    has_context = any(_term_pattern(term).search(normalize_text(combined).lower()) for term in context_terms)
    if has_context:
        return matches
    return [term for term in matches if term not in ambiguous_terms]

def journal_score(journal: str, config: dict[str, Any]) -> tuple[str, int]:
    target = normalize_journal(journal)
    for tier_name, tier in config["journals"].items():
        for item in tier["journals"]:
            aliases = item.get("aliases", []) + [item.get("canonical", "")]
            if any(target == normalize_journal(alias) for alias in aliases if alias):
                return tier_name.replace("tier_", "").upper(), int(tier["score"])
    return "unlisted", 0


def score_paper(paper: Paper, config: dict[str, Any]) -> ScoreResult:
    keyword_cfg = config["keywords"]
    core = keyword_cfg["core"]
    strong = keyword_cfg["strong"]
    exclude = keyword_cfg["exclude"]

    core_title = _non_overlapping_matches(paper.title, core["terms"])
    core_abstract = _non_overlapping_matches(paper.abstract_original, core["terms"])
    strong_title = _non_overlapping_matches(paper.title, strong["terms"])
    strong_abstract = _non_overlapping_matches(paper.abstract_original, strong["terms"])
    exclude_title = _non_overlapping_matches(paper.title, exclude["terms"])
    exclude_abstract = _non_overlapping_matches(paper.abstract_original, exclude["terms"])

    core_title = _remove_ambiguous_without_context(
        paper.title, paper.abstract_original, core_title, config
    )
    core_abstract = _remove_ambiguous_without_context(
        paper.title, paper.abstract_original, core_abstract, config
    )

    tier, j_score = journal_score(paper.journal, config)
    keyword_score = 0
    keyword_score += min(len(core_title) * int(core["title_score"]), int(core["title_cap"]))
    keyword_score += min(len(core_abstract) * int(core["abstract_score"]), int(core["abstract_cap"]))
    keyword_score += min(len(strong_title) * int(strong["title_score"]), int(strong["title_cap"]))
    keyword_score += min(len(strong_abstract) * int(strong["abstract_score"]), int(strong["abstract_cap"]))
    exclusion_penalty = 0
    exclusion_penalty += min(len(exclude_title) * int(exclude["title_penalty"]), int(exclude["title_cap"]))
    exclusion_penalty += min(len(exclude_abstract) * int(exclude["abstract_penalty"]), int(exclude["abstract_cap"]))
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
        exclude_title=exclude_title,
        exclude_abstract=exclude_abstract,
    )


def _slug(term: str) -> str:
    value = normalize_text(term).lower().replace("pi-", "")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def apply_score(paper: Paper, result: ScoreResult) -> None:
    paper.score = result.score
    paper.keyword_score = result.keyword_score
    paper.journal_score = result.journal_score
    paper.exclusion_penalty = result.exclusion_penalty
    paper.journal_tier = result.journal_tier
    paper.matched_core_title = result.core_title
    paper.matched_core_abstract = result.core_abstract
    paper.matched_strong_title = result.strong_title
    paper.matched_strong_abstract = result.strong_abstract
    paper.matched_exclude_title = result.exclude_title
    paper.matched_exclude_abstract = result.exclude_abstract

    positive = (
        result.core_title
        + result.core_abstract
        + result.strong_title
        + result.strong_abstract
    )
    unique_positive = list(dict.fromkeys(positive))
    paper.tags = [f"#{_slug(term)}" for term in unique_positive[:5] if _slug(term)]

    reasons: list[str] = []
    if result.core_title:
        reasons.append("タイトルでcore keyword「" + "，".join(result.core_title[:3]) + "」に一致")
    elif result.core_abstract:
        reasons.append("abstractでcore keyword「" + "，".join(result.core_abstract[:3]) + "」に一致")
    if result.strong_title:
        reasons.append("タイトルで関連語「" + "，".join(result.strong_title[:3]) + "」に一致")
    elif result.strong_abstract:
        reasons.append("abstractで関連語「" + "，".join(result.strong_abstract[:3]) + "」に一致")
    if result.journal_score:
        reasons.append(f"Journal tier {result.journal_tier}（+{result.journal_score}）")
    if result.exclude_title or result.exclude_abstract:
        exclusions = list(dict.fromkeys(result.exclude_title + result.exclude_abstract))
        reasons.append("除外語「" + "，".join(exclusions[:3]) + "」により減点")
    paper.reason_ja = "；".join(reasons) + "。" if reasons else "内容キーワードに一致しました。"
