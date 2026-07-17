from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Paper:
    openalex_id: str
    doi: str
    title: str
    authors: list[str]
    journal: str
    publication_date: str
    abstract_original: str
    landing_page_url: str
    score: int = 0
    keyword_score: int = 0
    journal_score: int = 0
    exclusion_penalty: int = 0
    journal_tier: str = "unlisted"
    matched_core_title: list[str] = field(default_factory=list)
    matched_core_abstract: list[str] = field(default_factory=list)
    matched_strong_title: list[str] = field(default_factory=list)
    matched_strong_abstract: list[str] = field(default_factory=list)
    matched_exclude_title: list[str] = field(default_factory=list)
    matched_exclude_abstract: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    article_image_path: str = ""
    article_image_method: str = ""

    @property
    def key(self) -> str:
        return self.doi.lower().strip() if self.doi else self.openalex_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Paper":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})
