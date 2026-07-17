from __future__ import annotations

from typing import Any

from .text_utils import normalize_journal, normalize_text


class ConfigError(ValueError):
    """Raised when config.yaml is incomplete or internally inconsistent."""


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping.")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list.")
    return value


def _require_number(value: Any, path: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a number.")
    if value < minimum:
        raise ConfigError(f"{path} must be at least {minimum}.")
    return float(value)


def _require_string_list(value: Any, path: str) -> list[str]:
    items = _require_list(value, path)
    if not items:
        raise ConfigError(f"{path} must not be empty.")
    cleaned: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{path}[{index}] must be a non-empty string.")
        cleaned.append(item.strip())
    return cleaned


def _normalized_term(term: str) -> str:
    return normalize_text(term).casefold()


def validate_config(config: dict[str, Any]) -> None:
    root = _require_mapping(config, "config")

    for key in (
        "timezone",
        "runtime",
        "posting",
        "journal_tiers",
        "keywords",
        "exclusion_keywords",
        "ai_summary",
    ):
        if key not in root:
            raise ConfigError(f"Missing required setting: {key}")

    if not isinstance(root["timezone"], str) or not root["timezone"].strip():
        raise ConfigError("timezone must be a non-empty string.")

    runtime = _require_mapping(root["runtime"], "runtime")
    for key in (
        "lookback_publication_days",
        "pages_per_query",
        "results_per_page",
        "slack_pause_seconds",
        "daily_target",
    ):
        if key not in runtime:
            raise ConfigError(f"Missing required setting: runtime.{key}")
        _require_number(runtime[key], f"runtime.{key}", minimum=0)

    if int(runtime["lookback_publication_days"]) < 1:
        raise ConfigError("runtime.lookback_publication_days must be at least 1.")
    if int(runtime["pages_per_query"]) < 1:
        raise ConfigError("runtime.pages_per_query must be at least 1.")
    if not 1 <= int(runtime["results_per_page"]) <= 100:
        raise ConfigError("runtime.results_per_page must be between 1 and 100.")

    posting = _require_mapping(root["posting"], "posting")
    for key in (
        "minimum_total_score",
        "minimum_keyword_score",
        "maximum_tags_displayed",
    ):
        if key not in posting:
            raise ConfigError(f"Missing required setting: posting.{key}")
        _require_number(posting[key], f"posting.{key}", minimum=0)
    if int(posting["maximum_tags_displayed"]) < 1:
        raise ConfigError("posting.maximum_tags_displayed must be at least 1.")

    tiers = _require_mapping(root["journal_tiers"], "journal_tiers")
    if not tiers:
        raise ConfigError("journal_tiers must not be empty.")

    seen_journal_aliases: dict[str, str] = {}
    for tier_name, raw_tier in tiers.items():
        tier = _require_mapping(raw_tier, f"journal_tiers.{tier_name}")
        if "score" not in tier or "journals" not in tier:
            raise ConfigError(
                f"journal_tiers.{tier_name} requires score and journals."
            )
        _require_number(tier["score"], f"journal_tiers.{tier_name}.score", minimum=0)
        journals = _require_list(tier["journals"], f"journal_tiers.{tier_name}.journals")
        for index, raw_item in enumerate(journals):
            item = _require_mapping(
                raw_item,
                f"journal_tiers.{tier_name}.journals[{index}]",
            )
            canonical = item.get("canonical")
            if not isinstance(canonical, str) or not canonical.strip():
                raise ConfigError(
                    f"journal_tiers.{tier_name}.journals[{index}].canonical "
                    "must be a non-empty string."
                )
            aliases = item.get("aliases", [])
            alias_values = _require_string_list(
                aliases or [canonical],
                f"journal_tiers.{tier_name}.journals[{index}].aliases",
            )
            for alias in [canonical, *alias_values]:
                normalized = normalize_journal(alias)
                owner = seen_journal_aliases.get(normalized)
                label = f"{tier_name}: {canonical}"
                if owner and owner != label:
                    raise ConfigError(
                        f"Journal alias {alias!r} is used in both {owner} and {label}."
                    )
                seen_journal_aliases[normalized] = label

    keyword_groups = _require_mapping(root["keywords"], "keywords")
    seen_terms: dict[str, str] = {}
    for group_name in ("core", "strong"):
        if group_name not in keyword_groups:
            raise ConfigError(f"Missing required setting: keywords.{group_name}")
        group = _require_mapping(keyword_groups[group_name], f"keywords.{group_name}")
        for score_key in ("title_score", "abstract_score", "title_cap", "abstract_cap"):
            if score_key not in group:
                raise ConfigError(
                    f"Missing required setting: keywords.{group_name}.{score_key}"
                )
            _require_number(
                group[score_key],
                f"keywords.{group_name}.{score_key}",
                minimum=0,
            )
        terms = _require_string_list(group.get("terms"), f"keywords.{group_name}.terms")
        for term in terms:
            normalized = _normalized_term(term)
            owner = seen_terms.get(normalized)
            if owner:
                raise ConfigError(
                    f"Keyword {term!r} is duplicated in {owner} and keywords.{group_name}."
                )
            seen_terms[normalized] = f"keywords.{group_name}"

    exclusion = _require_mapping(root["exclusion_keywords"], "exclusion_keywords")
    for score_key in ("title_penalty", "abstract_penalty", "title_cap", "abstract_cap"):
        if score_key not in exclusion:
            raise ConfigError(f"Missing required setting: exclusion_keywords.{score_key}")
        _require_number(
            exclusion[score_key],
            f"exclusion_keywords.{score_key}",
            minimum=0,
        )
    exclusion_terms = _require_string_list(
        exclusion.get("terms"),
        "exclusion_keywords.terms",
    )
    for term in exclusion_terms:
        normalized = _normalized_term(term)
        owner = seen_terms.get(normalized)
        if owner:
            raise ConfigError(
                f"Keyword {term!r} is duplicated in {owner} and exclusion_keywords."
            )
        seen_terms[normalized] = "exclusion_keywords"

    summary = _require_mapping(root["ai_summary"], "ai_summary")
    if not isinstance(summary.get("enabled"), bool) or not summary["enabled"]:
        raise ConfigError("ai_summary.enabled must be true.")
    if not isinstance(summary.get("model"), str) or not summary["model"].strip():
        raise ConfigError("ai_summary.model must be a non-empty string.")
    allowed_effort = {"none", "low", "medium", "high", "xhigh", "max"}
    if summary.get("reasoning_effort") not in allowed_effort:
        raise ConfigError(
            "ai_summary.reasoning_effort must be one of: "
            + ", ".join(sorted(allowed_effort))
        )
    for key in (
        "target_characters",
        "minimum_characters",
        "maximum_characters",
        "length_revision_attempts",
        "request_attempts",
        "request_timeout_seconds",
        "max_output_tokens",
    ):
        if key not in summary:
            raise ConfigError(f"Missing required setting: ai_summary.{key}")
        _require_number(summary[key], f"ai_summary.{key}", minimum=0)

    minimum = int(summary["minimum_characters"])
    target = int(summary["target_characters"])
    maximum = int(summary["maximum_characters"])
    if not 1 <= minimum <= target <= maximum:
        raise ConfigError(
            "ai_summary character settings must satisfy "
            "1 <= minimum_characters <= target_characters <= maximum_characters."
        )
    if int(summary["request_attempts"]) < 1:
        raise ConfigError("ai_summary.request_attempts must be at least 1.")
    if int(summary["max_output_tokens"]) < 64:
        raise ConfigError("ai_summary.max_output_tokens must be at least 64.")
