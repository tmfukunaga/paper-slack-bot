from __future__ import annotations

import logging
from urllib.parse import quote

import requests

from .text_utils import strip_markup

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://api.crossref.org/works"


def fetch_abstract(doi: str, contact_email: str = "") -> str:
    headers = {
        "User-Agent": f"PaperSlackBot/1.0 (mailto:{contact_email})" if contact_email else "PaperSlackBot/1.0"
    }
    try:
        response = requests.get(f"{BASE_URL}/{quote(doi, safe='')}", headers=headers, timeout=30)
        response.raise_for_status()
        item = response.json().get("message", {})
        return strip_markup(item.get("abstract", ""))
    except requests.RequestException as exc:
        LOGGER.warning("Crossref abstract fallback failed for %s: %s", doi, exc)
        return ""
