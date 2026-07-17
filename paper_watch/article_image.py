from __future__ import annotations

import io
import json
import logging
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .text_utils import doi_url, safe_has_public_http_url


LOGGER = logging.getLogger(__name__)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PaperSlackBot/1.0"
)


@dataclass(frozen=True)
class ImageCandidate:
    url: str
    score: int
    label: str = ""


@dataclass(frozen=True)
class ImageResult:
    path: Path
    method: str
    source_url: str = ""


def _srcset_best(value: str) -> str:
    choices: list[tuple[float, str]] = []
    for item in (value or "").split(","):
        parts = item.strip().split()
        if not parts:
            continue
        url = parts[0]
        weight = 1.0
        if len(parts) > 1:
            descriptor = parts[-1].lower()
            try:
                if descriptor.endswith("w"):
                    weight = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    weight = float(descriptor[:-1]) * 1000
            except ValueError:
                pass
        choices.append((weight, url))
    return max(choices, default=(0, ""))[1]


def _jsonld_image_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            image = value.get("image")
            if isinstance(image, str):
                found.append(urljoin(base_url, image))
            elif isinstance(image, dict):
                for key in ("url", "contentUrl"):
                    if isinstance(image.get(key), str):
                        found.append(urljoin(base_url, image[key]))
            elif isinstance(image, list):
                for item in image:
                    visit({"image": item})
            for key in ("@graph", "mainEntity", "hasPart"):
                if key in value:
                    visit(value[key])
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            visit(json.loads(script.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue

    return found


def _semantic_bonus(text: str) -> int:
    lowered = (text or "").lower()
    bonus = 0
    if any(
        token in lowered
        for token in (
            "graphical abstract",
            "graphical-abstract",
            "graphical_abstract",
            "toc graphic",
            "toc-graphic",
            "table of contents",
        )
    ):
        bonus += 900
    if any(
        token in lowered
        for token in (
            "article image",
            "article-image",
            "figure",
            "scheme",
            "hero",
            "featured",
            "abstract",
        )
    ):
        bonus += 250
    return bonus


def extract_html_candidates(html: str, base_url: str) -> list[ImageCandidate]:
    """Return ranked image candidates from static HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[ImageCandidate] = []

    dedicated = (
        ('meta[name="citation_graphical_abstract"]', "content", 1300),
        ('meta[name="dc.GraphicalAbstract"]', "content", 1300),
        ('meta[name="graphical-abstract"]', "content", 1300),
        ('meta[property="og:image"]', "content", 1000),
        ('meta[property="og:image:secure_url"]', "content", 1010),
        ('meta[name="twitter:image"]', "content", 950),
        ('meta[name="twitter:image:src"]', "content", 950),
    )
    for selector, attribute, score in dedicated:
        for tag in soup.select(selector):
            value = tag.get(attribute)
            if value:
                candidates.append(
                    ImageCandidate(
                        url=urljoin(base_url, value),
                        score=score + _semantic_bonus(str(tag)),
                        label=selector,
                    )
                )

    for url in _jsonld_image_urls(soup, base_url):
        candidates.append(
            ImageCandidate(url=url, score=800, label="json-ld image")
        )

    for image in soup.find_all("img"):
        src = (
            image.get("data-src")
            or image.get("data-original")
            or image.get("data-lazy-src")
            or _srcset_best(image.get("srcset", ""))
            or image.get("src")
        )
        if not src:
            continue

        attrs = " ".join(
            str(image.get(name, ""))
            for name in (
                "alt",
                "title",
                "class",
                "id",
                "src",
                "data-src",
            )
        )
        score = 200 + _semantic_bonus(attrs)
        try:
            width = int(float(image.get("width", 0) or 0))
            height = int(float(image.get("height", 0) or 0))
            if width and height:
                score += min(300, int(math.log10(width * height + 1) * 55))
        except (TypeError, ValueError):
            pass

        candidates.append(
            ImageCandidate(
                url=urljoin(base_url, src),
                score=score,
                label=attrs[:300],
            )
        )

    best: dict[str, ImageCandidate] = {}
    for candidate in candidates:
        if not safe_has_public_http_url(candidate.url):
            continue
        old = best.get(candidate.url)
        if old is None or candidate.score > old.score:
            best[candidate.url] = candidate

    return sorted(best.values(), key=lambda item: item.score, reverse=True)


class ArticleImageFetcher:
    """Find a representative article-page image and save it as a Slack-ready file."""

    def __init__(self, config: dict[str, Any]):
        self.cfg = config["article_image"]
        self._tempdir = tempfile.TemporaryDirectory(prefix="paper-watch-images-")
        self.root = Path(self._tempdir.name)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    def __enter__(self) -> "ArticleImageFetcher":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._tempdir.cleanup()

    def _start_browser(self) -> BrowserContext:
        if self._context is not None:
            return self._context

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={
                "width": int(self.cfg["viewport_width"]),
                "height": int(self.cfg["viewport_height"]),
            },
            java_script_enabled=True,
            ignore_https_errors=False,
        )
        return self._context

    def _rejected(self, url: str, label: str = "") -> bool:
        lowered_url = (url or "").lower()
        lowered_label = (label or "").lower()
        if any(
            term.lower() in lowered_url
            for term in self.cfg.get("reject_url_terms", [])
        ):
            return True
        if any(
            term.lower() in lowered_label
            for term in self.cfg.get("reject_text_terms", [])
        ):
            return True
        return False

    def _normalized_path(self, stem: str, suffix: str = ".jpg") -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-")
        return self.root / f"{safe[:80] or 'article-image'}{suffix}"

    def _save_image_bytes(
        self,
        data: bytes,
        stem: str,
        *,
        allow_small: bool = False,
    ) -> Path | None:
        try:
            with Image.open(io.BytesIO(data)) as original:
                image = ImageOps.exif_transpose(original)
                image.load()
                width, height = image.size

                if not allow_small:
                    if width < int(self.cfg["min_width"]):
                        return None
                    if height < int(self.cfg["min_height"]):
                        return None
                    if width * height < int(self.cfg["min_area"]):
                        return None

                max_width = int(self.cfg["max_output_width"])
                if width > max_width:
                    ratio = max_width / width
                    image = image.resize(
                        (max_width, max(1, int(height * ratio))),
                        Image.Resampling.LANCZOS,
                    )

                if image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    background.alpha_composite(rgba)
                    image = background.convert("RGB")
                else:
                    image = image.convert("RGB")

                path = self._normalized_path(stem, ".jpg")
                image.save(
                    path,
                    format="JPEG",
                    quality=int(self.cfg["jpeg_quality"]),
                    optimize=True,
                )
                return path
        except Exception as exc:
            LOGGER.debug("Could not normalize image bytes: %s", exc)
            return None

    def _download_candidate(
        self,
        candidate: ImageCandidate,
        referer: str,
        stem: str,
    ) -> Path | None:
        if self._rejected(candidate.url, candidate.label):
            return None

        headers = {"User-Agent": USER_AGENT, "Referer": referer}
        try:
            response = requests.get(
                candidate.url,
                headers=headers,
                timeout=int(self.cfg["request_timeout_seconds"]),
                stream=True,
            )
            response.raise_for_status()
            buffer = io.BytesIO()
            max_bytes = int(self.cfg["max_download_bytes"])
            for chunk in response.iter_content(65536):
                if chunk:
                    buffer.write(chunk)
                if buffer.tell() > max_bytes:
                    return None
            return self._save_image_bytes(buffer.getvalue(), stem)
        except requests.RequestException as exc:
            LOGGER.debug("Image download failed %s: %s", candidate.url, exc)
            return None

    def _requests_fetch(self, url: str, stem: str) -> ImageResult | None:
        try:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=int(self.cfg["request_timeout_seconds"]),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            LOGGER.info("Static article-page request failed for %s: %s", url, exc)
            return None

        for index, candidate in enumerate(
            extract_html_candidates(response.text, response.url)
        ):
            path = self._download_candidate(
                candidate,
                response.url,
                f"{stem}-static-{index}",
            )
            if path is not None:
                return ImageResult(
                    path=path,
                    method="page metadata/image",
                    source_url=candidate.url,
                )
        return None

    def _browser_image_records(self, page: Page) -> list[dict[str, Any]]:
        return page.locator("img").evaluate_all(
            """
            (images) => images.map((img, index) => {
              const rect = img.getBoundingClientRect();
              return {
                index,
                url: img.currentSrc || img.src || '',
                alt: img.alt || '',
                title: img.title || '',
                className: typeof img.className === 'string' ? img.className : '',
                id: img.id || '',
                naturalWidth: img.naturalWidth || 0,
                naturalHeight: img.naturalHeight || 0,
                displayWidth: Math.max(0, rect.width || 0),
                displayHeight: Math.max(0, rect.height || 0),
                visible: !!(rect.width && rect.height)
              };
            })
            """
        )

    def _browser_candidate_score(self, record: dict[str, Any]) -> int:
        text = " ".join(
            str(record.get(key, ""))
            for key in ("url", "alt", "title", "className", "id")
        )
        score = _semantic_bonus(text)
        natural_area = int(record.get("naturalWidth", 0)) * int(
            record.get("naturalHeight", 0)
        )
        display_area = float(record.get("displayWidth", 0)) * float(
            record.get("displayHeight", 0)
        )
        if natural_area:
            score += min(500, int(math.log10(natural_area + 1) * 75))
        if display_area:
            score += min(300, int(math.log10(display_area + 1) * 55))
        return score

    def _browser_element_screenshot(
        self,
        page: Page,
        records: list[dict[str, Any]],
        stem: str,
    ) -> ImageResult | None:
        ranked = sorted(
            records,
            key=self._browser_candidate_score,
            reverse=True,
        )
        min_width = int(self.cfg["min_width"])
        min_height = int(self.cfg["min_height"])
        min_area = int(self.cfg["min_area"])

        for rank, record in enumerate(ranked[:25]):
            label = " ".join(
                str(record.get(key, ""))
                for key in ("alt", "title", "className", "id")
            )
            url = str(record.get("url", ""))
            if self._rejected(url, label):
                continue
            natural_width = int(record.get("naturalWidth", 0))
            natural_height = int(record.get("naturalHeight", 0))
            if natural_width < min_width or natural_height < min_height:
                continue
            if natural_width * natural_height < min_area:
                continue
            if not record.get("visible"):
                continue

            locator = page.locator("img").nth(int(record["index"]))
            try:
                locator.scroll_into_view_if_needed(timeout=5000)
                raw = locator.screenshot(
                    type="png",
                    animations="disabled",
                    timeout=10000,
                )
                path = self._save_image_bytes(
                    raw,
                    f"{stem}-browser-image-{rank}",
                )
                if path is not None:
                    return ImageResult(
                        path=path,
                        method="rendered page image",
                        source_url=url,
                    )
            except Exception as exc:
                LOGGER.debug("Element screenshot failed: %s", exc)
        return None

    def _remove_page_overlays(self, page: Page) -> None:
        try:
            page.evaluate(
                """
                () => {
                  const terms = /cookie|consent|privacy|gdpr|modal|overlay|subscribe|newsletter/i;
                  for (const el of document.querySelectorAll('body *')) {
                    const text = `${el.id || ''} ${el.className || ''} ${el.getAttribute('aria-label') || ''}`;
                    if (!terms.test(text)) continue;
                    const style = getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'sticky') {
                      el.remove();
                    }
                  }
                }
                """
            )
        except Exception:
            pass

    def _browser_fetch(self, url: str, stem: str) -> ImageResult | None:
        try:
            context = self._start_browser()
        except Exception as exc:
            LOGGER.warning("Playwright could not start: %s", exc)
            return None

        page = context.new_page()
        response_status: int | None = None
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.cfg["browser_timeout_seconds"]) * 1000,
            )
            if response is not None:
                response_status = response.status
            page.wait_for_timeout(int(self.cfg["browser_wait_milliseconds"]))

            # First try metadata discovered after JavaScript rendering.
            for index, candidate in enumerate(
                extract_html_candidates(page.content(), page.url)
            ):
                path = self._download_candidate(
                    candidate,
                    page.url,
                    f"{stem}-browser-meta-{index}",
                )
                if path is not None:
                    return ImageResult(
                        path=path,
                        method="rendered metadata/image",
                        source_url=candidate.url,
                    )

            result = self._browser_element_screenshot(
                page,
                self._browser_image_records(page),
                stem,
            )
            if result is not None:
                return result

            if not self.cfg.get("screenshot_fallback", True):
                return None
            if response_status is not None and response_status >= 400:
                LOGGER.info(
                    "Skipping page screenshot because HTTP status was %s: %s",
                    response_status,
                    page.url,
                )
                return None

            body_text = page.locator("body").inner_text(timeout=5000)[:1000].lower()
            if any(
                marker in body_text
                for marker in (
                    "access denied",
                    "forbidden",
                    "captcha",
                    "temporarily unavailable",
                )
            ):
                return None

            self._remove_page_overlays(page)
            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(300)
            except Exception:
                pass
            raw = page.screenshot(
                type="jpeg",
                quality=int(self.cfg["jpeg_quality"]),
                full_page=False,
                animations="disabled",
            )
            path = self._save_image_bytes(
                raw,
                f"{stem}-page-screenshot",
                allow_small=True,
            )
            if path is not None:
                return ImageResult(
                    path=path,
                    method="article-page screenshot",
                    source_url=page.url,
                )
            return None
        except PlaywrightTimeoutError as exc:
            LOGGER.info("Browser timed out for %s: %s", url, exc)
            return None
        except Exception as exc:
            LOGGER.info("Browser image retrieval failed for %s: %s", url, exc)
            return None
        finally:
            page.close()

    def _load_font(self, size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = []
        if bold:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ])
        else:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            ])
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _wrap_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
        max_lines: int,
    ) -> list[str]:
        words = (text or '').split()
        if not words:
            return []
        lines: list[str] = []
        current = words[0]
        index = 1
        while index < len(words):
            word = words[index]
            trial = f"{current} {word}"
            if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
                current = trial
                index += 1
                continue
            lines.append(current)
            current = word
            index += 1
            if len(lines) >= max_lines - 1:
                break
        remaining = [current] + words[index:]
        last_line = ' '.join(remaining).strip()
        if last_line:
            lines.append(last_line)
        lines = lines[:max_lines]
        if len(lines) == max_lines:
            while draw.textbbox((0, 0), lines[-1], font=font)[2] > max_width and ' ' in lines[-1]:
                lines[-1] = ' '.join(lines[-1].split()[:-1]) + '…'
            if draw.textbbox((0, 0), lines[-1], font=font)[2] > max_width:
                lines[-1] = '…'
        return lines

    def _generate_fallback_card(
        self,
        title: str,
        journal: str,
        publication_date: str,
        doi: str,
        stem: str,
    ) -> ImageResult:
        width = 1400
        height = 900
        margin = 80
        bg = '#f7f8fb'
        accent = '#3a5ad9'
        text_color = '#111827'
        sub_color = '#374151'
        mute_color = '#6b7280'

        image = Image.new('RGB', (width, height), bg)
        draw = ImageDraw.Draw(image)

        draw.rounded_rectangle((margin, 48, width - margin, 66), radius=9, fill=accent)
        draw.rounded_rectangle((margin, height - 72, width - margin, height - 54), radius=9, fill='#d6ddff')

        title_font = self._load_font(54, bold=True)
        meta_font = self._load_font(34, bold=False)
        small_font = self._load_font(28, bold=False)
        doi_font = self._load_font(30, bold=True)

        y = 110
        max_text_width = width - 2 * margin

        title_lines = self._wrap_text(draw, title or 'Untitled article', title_font, max_text_width, 6)
        for line in title_lines:
            draw.text((margin, y), line, font=title_font, fill=text_color)
            y += 68

        y += 18
        for line in self._wrap_text(draw, (journal or 'Source unavailable').strip(), meta_font, max_text_width, 2):
            draw.text((margin, y), line, font=meta_font, fill=sub_color)
            y += 44

        draw.text((margin, y + 6), (publication_date or 'Date unavailable').strip(), font=small_font, fill=mute_color)
        y += 74

        draw.rounded_rectangle((margin, y, width - margin, y + 170), radius=24, fill='white', outline='#dbe2f0', width=2)
        draw.text((margin + 26, y + 24), 'DOI', font=small_font, fill=mute_color)
        yy = y + 66
        for line in self._wrap_text(draw, (doi or 'Unavailable').strip(), doi_font, max_text_width - 52, 3):
            draw.text((margin + 26, yy), line, font=doi_font, fill=text_color)
            yy += 38

        footer = 'Auto-generated article card (page image fallback)'
        bbox = draw.textbbox((0, 0), footer, font=small_font)
        draw.text((width - margin - (bbox[2] - bbox[0]), height - 120), footer, font=small_font, fill=mute_color)

        path = self._normalized_path(f"{stem}-fallback-card", '.jpg')
        image.save(path, format='JPEG', quality=int(self.cfg['jpeg_quality']), optimize=True)
        return ImageResult(path=path, method='generated fallback card', source_url='')

    def fetch(
        self,
        article_url: str,
        doi: str,
        key: str,
        *,
        title: str = "",
        journal: str = "",
        publication_date: str = "",
    ) -> ImageResult:
        """Return an image for every paper.

        Page-image retrieval is best effort. Any retrieval failure falls back to
        a locally generated article card, so callers never receive ``None``.
        """
        stem = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-") or "paper"

        try:
            urls: list[str] = []
            for value in (article_url, doi_url(doi)):
                if safe_has_public_http_url(value) and value not in urls:
                    urls.append(value)

            if self.cfg.get("enabled", True):
                for url in urls:
                    result = self._requests_fetch(url, stem)
                    if result is not None:
                        LOGGER.info(
                            "Article image found by %s for %s",
                            result.method,
                            doi,
                        )
                        return result

                for url in urls:
                    result = self._browser_fetch(url, stem)
                    if result is not None:
                        LOGGER.info(
                            "Article image found by %s for %s",
                            result.method,
                            doi,
                        )
                        return result
        except Exception as exc:
            LOGGER.warning(
                "Unexpected article-image retrieval error for %s: %s. "
                "Generating fallback card.",
                doi,
                exc,
            )

        LOGGER.info("No article image found for %s; generating fallback card", doi)
        return self._generate_fallback_card(
            title=title,
            journal=journal,
            publication_date=publication_date,
            doi=doi,
            stem=stem,
        )
