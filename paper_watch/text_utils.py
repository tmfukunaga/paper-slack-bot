from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import urlparse

from bs4 import BeautifulSoup


DASHES = "‐‑‒–—―−"


def normalize_text(text: str) -> str:
    text = html.unescape(text or "")
    text = unicodedata.normalize("NFKC", text)
    for dash in DASHES:
        text = text.replace(dash, "-")
    text = text.replace("π", "pi")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_journal(text: str) -> str:
    text = normalize_text(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_markup(text: str) -> str:
    """Remove HTML/XML tags such as <scp> while preserving visible text."""
    if not text:
        return ""
    soup = BeautifulSoup(html.unescape(text), "html.parser")
    return normalize_text(soup.get_text(" ", strip=True))


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    if not inverted_index:
        return ""
    max_position = max((max(pos) for pos in inverted_index.values() if pos), default=-1)
    if max_position < 0:
        return ""
    words = [""] * (max_position + 1)
    for token, positions in inverted_index.items():
        for position in positions:
            if 0 <= position < len(words):
                words[position] = token
    return strip_markup(" ".join(word for word in words if word))


def normalize_doi(value: str) -> str:
    value = normalize_text(value).lower()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value)
    return value.strip()


def doi_url(doi: str) -> str:
    return f"https://doi.org/{normalize_doi(doi)}" if doi else ""


def split_for_slack(text: str, limit: int = 2800) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind(". ", 0, limit),
            remaining.rfind("; ", 0, limit),
        )
        if cut < int(limit * 0.55):
            cut = remaining.rfind(" ", 0, limit)
        if cut < int(limit * 0.4):
            cut = limit
        else:
            cut += 1
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def safe_has_public_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def escape_slack_mrkdwn(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
