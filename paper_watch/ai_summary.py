from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)
RESPONSES_URL = "https://api.openai.com/v1/responses"


class SummaryError(RuntimeError):
    """Raised when a reliable Japanese summary cannot be generated."""


@dataclass(frozen=True)
class SummaryResult:
    text: str
    model: str
    character_count: int


def summary_character_count(text: str) -> int:
    """Count visible characters after normalizing whitespace."""
    return len(re.sub(r"\s+", " ", text or "").strip())


def clean_summary(text: str) -> str:
    """Remove common wrappers while preserving chemistry notation."""
    value = (text or "").strip()
    value = re.sub(r"^```(?:text|markdown)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    value = re.sub(r"^(?:要約|日本語要約|Summary)\s*[:：]\s*", "", value, flags=re.I)
    value = value.replace("**", "")
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) >= 2 and value[0] in "\"'“”『「" and value[-1] in "\"'“”』」":
        value = value[1:-1].strip()
    return value


def extract_output_text(payload: dict[str, Any]) -> str:
    """Extract text from a Responses API JSON response."""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts).strip()


def build_summary_instructions(config: dict[str, Any]) -> str:
    summary = config["ai_summary"]
    minimum = int(summary["minimum_characters"])
    maximum = int(summary["maximum_characters"])
    target = int(summary["target_characters"])

    return (
        "あなたは有機化学・物理有機化学・材料化学に精通した学術編集者です。"
        "与えられた論文タイトルとAbstractだけを根拠に、日本語で一文の要約を作成してください。\n"
        f"- 目標は{target}字、許容範囲は{minimum}～{maximum}字です。\n"
        "- 研究対象、何を行ったか、主要な結果または意義を、情報がある範囲で含めてください。\n"
        "- Abstractにない内容、推測、誇張、評価を追加しないでください。\n"
        "- 化合物名、材料名、分子名、反応名、略語、分子式は英語表記のままで構いません。"
        "不自然な日本語訳を作らないでください。\n"
        "- 数値、符号、化学式、立体化学表記は原文を尊重してください。\n"
        "- 『本研究では』などの定型的な導入、見出し、箇条書き、引用符、注釈は不要です。\n"
        "- 出力は要約本文だけにしてください。"
    )


class OpenAISummarizer:
    def __init__(self, api_key: str, config: dict[str, Any]) -> None:
        if not api_key.strip():
            raise SummaryError("OPENAI_API_KEY is empty.")
        self.api_key = api_key.strip()
        self.config = config
        self.settings = config["ai_summary"]
        self.instructions = build_summary_instructions(config)

    def _request(self, user_input: str) -> dict[str, Any]:
        attempts = int(self.settings["request_attempts"])
        timeout = float(self.settings["request_timeout_seconds"])
        waits = [5, 15, 30, 60]

        body: dict[str, Any] = {
            "model": self.settings["model"],
            "instructions": self.instructions,
            "input": user_input,
            "reasoning": {"effort": self.settings["reasoning_effort"]},
            "max_output_tokens": int(self.settings["max_output_tokens"]),
            "store": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = ""
        for attempt in range(attempts):
            try:
                response = requests.post(
                    RESPONSES_URL,
                    headers=headers,
                    json=body,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt + 1 >= attempts:
                    break
                wait_seconds = waits[min(attempt, len(waits) - 1)]
                LOGGER.warning(
                    "OpenAI request failed; retrying in %s seconds: %s",
                    wait_seconds,
                    exc,
                )
                time.sleep(wait_seconds)
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise SummaryError("OpenAI returned invalid JSON.") from exc

            try:
                error_payload = response.json()
                error_info = error_payload.get("error", {})
                error_message = str(error_info.get("message") or error_payload)[:500]
            except ValueError:
                error_message = response.text[:500]

            last_error = f"HTTP {response.status_code}: {error_message}"

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt + 1 >= attempts:
                    break
                retry_after = response.headers.get("Retry-After", "")
                wait_seconds = (
                    int(retry_after)
                    if retry_after.isdigit()
                    else waits[min(attempt, len(waits) - 1)]
                )
                wait_seconds = min(wait_seconds, 120)
                LOGGER.warning(
                    "OpenAI returned %s; retrying in %s seconds.",
                    response.status_code,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            raise SummaryError(last_error)

        raise SummaryError(f"OpenAI request failed after retries: {last_error}")

    def _generate(self, user_input: str) -> str:
        payload = self._request(user_input)
        text = clean_summary(extract_output_text(payload))
        if not text:
            status = payload.get("status", "unknown")
            incomplete = payload.get("incomplete_details")
            raise SummaryError(
                f"OpenAI returned no summary text (status={status}, incomplete={incomplete})."
            )
        return text

    def summarize(self, title: str, abstract: str, doi: str = "") -> SummaryResult:
        if not abstract.strip():
            raise SummaryError("Abstract is empty; summary was not generated.")

        minimum = int(self.settings["minimum_characters"])
        maximum = int(self.settings["maximum_characters"])
        target = int(self.settings["target_characters"])
        revision_attempts = int(self.settings["length_revision_attempts"])

        source = (
            f"Title:\n{title.strip()}\n\n"
            f"Abstract:\n{abstract.strip()}\n\n"
            f"DOI:\n{doi.strip() or 'not provided'}"
        )
        candidates: list[str] = []
        draft = self._generate(source)
        candidates.append(draft)

        for _ in range(revision_attempts):
            count = summary_character_count(draft)
            if minimum <= count <= maximum:
                break
            draft = self._generate(
                source
                + "\n\nPrevious draft:\n"
                + draft
                + f"\n\nThe previous draft was {count} characters. "
                + f"Rewrite it to approximately {target} Japanese characters "
                + f"and keep it within {minimum}–{maximum} characters."
            )
            candidates.append(draft)

        best = min(
            candidates,
            key=lambda value: abs(summary_character_count(value) - target),
        )
        best_count = summary_character_count(best)
        if not minimum <= best_count <= maximum:
            LOGGER.warning(
                "Summary length outside requested range: DOI=%s chars=%s range=%s-%s",
                doi,
                best_count,
                minimum,
                maximum,
            )

        return SummaryResult(
            text=best,
            model=str(self.settings["model"]),
            character_count=best_count,
        )
