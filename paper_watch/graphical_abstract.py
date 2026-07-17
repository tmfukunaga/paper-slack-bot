from __future__ import annotations

import io
import json
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

from .text_utils import safe_has_public_http_url

LOGGER = logging.getLogger(__name__)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PaperSlackBot/1.0; +https://github.com/)"
}


def _extract_jsonld_images(soup: BeautifulSoup, base_url: str) -> list[str]:
    images: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            image = node.get("image")
            values = image if isinstance(image, list) else [image]
            for value in values:
                if isinstance(value, str):
                    images.append(urljoin(base_url, value))
                elif isinstance(value, dict) and isinstance(value.get("url"), str):
                    images.append(urljoin(base_url, value["url"]))
    return images


def _candidate_urls(soup: BeautifulSoup, base_url: str) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    dedicated_selectors = [
        ('meta[name="citation_graphical_abstract"]', "content"),
        ('meta[name="dc.GraphicalAbstract"]', "content"),
        ('meta[name="graphical-abstract"]', "content"),
    ]
    for selector, attr in dedicated_selectors:
        for tag in soup.select(selector):
            if tag.get(attr):
                candidates.append((100, urljoin(base_url, tag[attr])))

    for url in _extract_jsonld_images(soup, base_url):
        candidates.append((45, url))

    generic_selectors = [
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[name="twitter:image:src"]', "content"),
    ]
    for selector, attr in generic_selectors:
        for tag in soup.select(selector):
            if tag.get(attr):
                candidates.append((25, urljoin(base_url, tag[attr])))

    for img in soup.find_all("img"):
        attrs = " ".join(
            str(img.get(name, "")) for name in ("class", "id", "alt", "title", "src")
        ).lower()
        if any(token in attrs for token in ("graphical abstract", "graphical-abstract", "toc graphic", "toc-graphic")):
            src = img.get("src") or img.get("data-src")
            if src:
                candidates.append((90, urljoin(base_url, src)))

    deduped: dict[str, int] = {}
    for base_score, url in candidates:
        lowered = url.lower()
        bonus = 30 if any(token in lowered for token in ("graphical", "toc", "abstract", "ga_", "ga-")) else 0
        deduped[url] = max(deduped.get(url, 0), base_score + bonus)
    return sorted(((score, url) for url, score in deduped.items()), reverse=True)


def _valid_image(url: str, cfg: dict) -> bool:
    if not safe_has_public_http_url(url):
        return False
    lowered = url.lower()
    if any(term.lower() in lowered for term in cfg.get("reject_url_terms", [])):
        return False
    try:
        response = requests.get(url, headers=HEADERS, timeout=cfg["timeout_seconds"], stream=True)
        response.raise_for_status()
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            return False
        buffer = io.BytesIO()
        max_bytes = int(cfg["max_download_bytes"])
        for chunk in response.iter_content(65536):
            buffer.write(chunk)
            if buffer.tell() > max_bytes:
                return False
        buffer.seek(0)
        with Image.open(buffer) as image:
            width, height = image.size
        return width >= int(cfg["min_width"]) and height >= int(cfg["min_height"])
    except Exception as exc:
        LOGGER.debug("Rejected image %s: %s", url, exc)
        return False


def find_graphical_abstract(article_url: str, config: dict) -> str:
    cfg = config["graphical_abstract"]
    if not cfg.get("enabled") or not article_url:
        return ""
    try:
        response = requests.get(article_url, headers=HEADERS, timeout=cfg["timeout_seconds"])
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except requests.RequestException as exc:
        LOGGER.info("Article page unavailable for graphical abstract: %s", exc)
        return ""

    article_host = urlparse(response.url).netloc
    for score, url in _candidate_urls(soup, response.url):
        if score < int(cfg.get("minimum_candidate_score", 45)):
            continue
        LOGGER.debug("GA candidate score=%s host=%s url=%s", score, article_host, url)
        if _valid_image(url, cfg):
            return url
    return ""
