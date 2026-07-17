from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)
RESPONSES_URL = "https://api.openai.com/v1/responses"


class SummaryError(RuntimeError):
    """Raised when neither an AI summary nor a safe fallback can be produced."""


class OpenAIRequestError(SummaryError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SummaryResult:
    text: str
    model: str
    character_count: int
    language: str
    used_fallback: bool = False


def summary_character_count(text: str) -> int:
    """Count visible characters after normalizing whitespace."""
    return len(re.sub(r"\s+", " ", text or "").strip())


def clean_summary(text: str) -> str:
    """Remove common wrappers while preserving chemistry notation."""
    value = (text or "").strip()
    value = re.sub(r"^```(?:text|markdown|json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    value = re.sub(
        r"^(?:要約|日本語要約|Summary|English summary)\s*[:：]\s*",
        "",
        value,
        flags=re.I,
    )
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


def extract_structured_summaries(payload: dict[str, Any]) -> tuple[str, str]:
    """Read Japanese and English summaries from a structured response."""
    raw = extract_output_text(payload)
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("OpenAI structured output was not valid JSON: %s", raw[:300])
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return (
        clean_summary(str(data.get("summary_ja") or "")),
        clean_summary(str(data.get("summary_en") or "")),
    )


def build_summary_instructions(config: dict[str, Any]) -> str:
    summary = config["ai_summary"]
    minimum = int(summary["minimum_characters"])
    maximum = int(summary["maximum_characters"])
    target = int(summary["target_characters"])

    return (
        "あなたは有機化学・物理有機化学・材料化学に精通した学術編集者です。"
        "論文タイトルとAbstractだけを根拠に、研究内容を正確かつ自然に圧縮してください。\n"
        "日本語要約の規則:\n"
        f"1. {target}字程度（許容範囲{minimum}～{maximum}字）で、一文または二文の完結した要約にする。\n"
        "2. 研究対象、主要な方法または設計上の新規性、最重要の結果を必ず含める。"
        "意義はAbstractに明記されている場合だけ含める。\n"
        "3. 字数が足りない場合は背景説明を削り、研究対象・実施内容・主要結果を優先する。\n"
        "4. 文を途中で切らない。名詞句や接続助詞で終えず、最後は必ず句点『。』で閉じる。\n"
        "5. Abstractにない推測、誇張、評価、因果関係を追加しない。\n"
        "6. 化合物名、材料名、分子名、反応名、略語、分子式は原文の英語表記のままで構いません。"
        "ただし、助詞や述語を自然につなぎ、逐語訳調の不自然な日本語にしない。\n"
        "7. 数値、単位、符号、化学式、立体化学表記は原文を尊重する。"
        "数値は最重要なものだけを残す。\n"
        "8. 『本研究では』などの定型句、見出し、箇条書き、引用符、注釈は不要。\n"
        "9. 出力前に、主語と述語の対応、修飾関係、助詞、文末の完結性を内部で確認する。"
        "英語の専門語を並べただけの文、数値や名詞句で終わる文、逐語訳調の文は不可。\n"
        "英語要約の規則:\n"
        "10. summary_enには、同じ内容を40～70 wordsの自然で完結した英語で記す。"
        "日本語要約が検証に通らない場合の予備として使う。\n"
        "11. 指定されたJSON以外は出力しない。"
    )


def japanese_summary_issues(
    text: str,
    minimum: int,
    maximum: int,
) -> list[str]:
    """Return reasons why a Japanese summary is unsafe to post."""
    value = clean_summary(text)
    issues: list[str] = []
    count = summary_character_count(value)
    if count < minimum:
        issues.append(f"too short ({count} < {minimum})")
    if count > maximum:
        issues.append(f"too long ({count} > {maximum})")
    if not value.endswith("。"):
        issues.append("does not end with Japanese full stop")
    if len(re.findall(r"[ぁ-んァ-ヶ一-龯]", value)) < 10:
        issues.append("insufficient Japanese text")
    if re.search(r"(?:ABSTRACT|Title:|Abstract:|DOI:)", value, flags=re.I):
        issues.append("contains source-field label")
    if value.count("(") != value.count(")") or value.count("[") != value.count("]"):
        issues.append("unbalanced brackets")
    if re.search(r"(?:、|・|:|：|;|；|\b(?:and|or))\s*。?$", value, flags=re.I):
        issues.append("appears to end mid-list")
    if re.search(r"(?:および|または|ならびに|による|により|として|を通じて|において)\s*。?$", value):
        issues.append("appears to end with a connective phrase")
    return issues


def english_summary_is_valid(text: str) -> bool:
    value = clean_summary(text)
    words = re.findall(r"\b[\w'’-]+\b", value)
    return (
        20 <= len(words) <= 100
        and value.endswith((".", "!", "?"))
        and not re.search(r"(?:ABSTRACT|Title:|Abstract:|DOI:)", value, flags=re.I)
    )


def extractive_english_fallback(abstract: str, maximum: int = 420) -> str:
    """Create a deterministic, non-hallucinatory English fallback excerpt."""
    value = re.sub(r"^\s*ABSTRACT\s*", "", abstract or "", flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return ""

    sentences = re.split(r"(?<=[.!?])\s+", value)
    selected = ""
    for sentence in sentences:
        candidate = sentence.strip()
        if not candidate:
            continue
        joined = f"{selected} {candidate}".strip()
        if len(joined) > maximum:
            break
        selected = joined
        if len(selected) >= 160:
            break

    if selected:
        if not selected.endswith((".", "!", "?")):
            selected += "."
        return selected

    clipped = value[: maximum - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return clipped + "…"


def _response_usage(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    output = int(usage.get("output_tokens") or 0)
    details = usage.get("output_tokens_details") or {}
    reasoning = int(details.get("reasoning_tokens") or 0)
    return output, reasoning


class OpenAISummarizer:
    def __init__(self, api_key: str, config: dict[str, Any]) -> None:
        if not api_key.strip():
            raise SummaryError("OPENAI_API_KEY is empty.")
        self.api_key = api_key.strip()
        self.config = config
        self.settings = config["ai_summary"]
        self.instructions = build_summary_instructions(config)

    def _response_schema(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "name": "paper_summary",
            "description": "Japanese research summary plus an English fallback summary.",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "summary_ja": {
                        "type": "string",
                        "description": "Complete natural Japanese summary ending with 。",
                    },
                    "summary_en": {
                        "type": "string",
                        "description": "Complete English fallback summary of 40 to 70 words.",
                    },
                },
                "required": ["summary_ja", "summary_en"],
                "additionalProperties": False,
            },
        }

    def _request(
        self,
        user_input: str,
        *,
        effort: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        attempts = int(self.settings["request_attempts"])
        timeout = float(self.settings["request_timeout_seconds"])
        waits = [5, 15, 30, 60]

        body: dict[str, Any] = {
            "model": self.settings["model"],
            "instructions": self.instructions,
            "input": user_input,
            "reasoning": {"effort": effort},
            "max_output_tokens": max_output_tokens,
            "text": {
                "verbosity": "low",
                "format": self._response_schema(),
            },
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

            try:
                payload = response.json()
            except ValueError:
                payload = {}

            if response.status_code == 200:
                if not isinstance(payload, dict):
                    raise SummaryError("OpenAI returned invalid JSON.")
                return payload

            error_info = payload.get("error", {}) if isinstance(payload, dict) else {}
            error_message = str(error_info.get("message") or response.text or payload)[:500]
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

            raise OpenAIRequestError(response.status_code, last_error)

        raise SummaryError(f"OpenAI request failed after retries: {last_error}")

    def _generate_pair(self, user_input: str) -> tuple[str, str]:
        primary_effort = str(self.settings["reasoning_effort"])
        fallback_effort = str(self.settings.get("fallback_reasoning_effort", "low"))
        primary_budget = int(self.settings["max_output_tokens"])
        retry_budget = int(self.settings["retry_max_output_tokens"])

        specs: list[tuple[str, int]] = [
            (primary_effort, primary_budget),
            (primary_effort, retry_budget),
        ]
        if fallback_effort != primary_effort:
            specs.append((fallback_effort, retry_budget))

        unique_specs: list[tuple[str, int]] = []
        for spec in specs:
            if spec not in unique_specs:
                unique_specs.append(spec)

        last_problem = ""
        for effort, budget in unique_specs:
            try:
                payload = self._request(
                    user_input,
                    effort=effort,
                    max_output_tokens=budget,
                )
            except OpenAIRequestError as exc:
                # Some model snapshots may reject a particular effort level.
                if exc.status_code == 400 and "effort" in str(exc).lower():
                    LOGGER.warning(
                        "OpenAI rejected reasoning effort=%s; trying fallback effort.",
                        effort,
                    )
                    last_problem = str(exc)
                    continue
                raise

            status = str(payload.get("status") or "unknown")
            incomplete = payload.get("incomplete_details") or {}
            reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
            output_tokens, reasoning_tokens = _response_usage(payload)

            LOGGER.info(
                "OpenAI summary response status=%s effort=%s max_output_tokens=%s "
                "output_tokens=%s reasoning_tokens=%s",
                status,
                effort,
                budget,
                output_tokens,
                reasoning_tokens,
            )

            if status == "incomplete" and reason == "max_output_tokens":
                last_problem = (
                    f"max_output_tokens reached at {budget} "
                    f"(reasoning_tokens={reasoning_tokens})"
                )
                LOGGER.warning("%s; retrying with a larger/safer configuration.", last_problem)
                continue

            japanese, english = extract_structured_summaries(payload)
            if japanese or english:
                return japanese, english

            last_problem = f"no structured summary text (status={status})"
            LOGGER.warning("OpenAI returned %s; trying another configuration.", last_problem)

        raise SummaryError(f"OpenAI generated no usable summary: {last_problem}")

    def summarize(self, title: str, abstract: str, doi: str = "") -> SummaryResult:
        if not abstract.strip():
            raise SummaryError("Abstract is empty; summary was not generated.")

        minimum = int(self.settings["minimum_characters"])
        maximum = int(self.settings["maximum_characters"])
        target = int(self.settings["target_characters"])
        revision_attempts = int(self.settings["quality_revision_attempts"])

        source = (
            f"Title:\n{title.strip()}\n\n"
            f"Abstract:\n{abstract.strip()}\n\n"
            f"DOI:\n{doi.strip() or 'not provided'}"
        )

        japanese_candidates: list[str] = []
        english_candidates: list[str] = []
        try:
            japanese, english = self._generate_pair(source)
            if japanese:
                japanese_candidates.append(japanese)
            if english:
                english_candidates.append(english)

            for _ in range(revision_attempts):
                if japanese_candidates:
                    issues = japanese_summary_issues(
                        japanese_candidates[-1],
                        minimum,
                        maximum,
                    )
                    if not issues:
                        break
                else:
                    issues = ["Japanese summary was empty"]

                repair_request = (
                    source
                    + "\n\nPrevious Japanese draft:\n"
                    + (japanese_candidates[-1] if japanese_candidates else "(empty)")
                    + "\n\nProblems to fix:\n- "
                    + "\n- ".join(issues)
                    + f"\n\nRewrite it as a complete, natural Japanese summary near {target} characters. "
                    + "Do not merely shorten by cutting the ending."
                )
                repaired_ja, repaired_en = self._generate_pair(repair_request)
                if repaired_ja:
                    japanese_candidates.append(repaired_ja)
                if repaired_en:
                    english_candidates.append(repaired_en)

        except SummaryError as exc:
            LOGGER.error("AI summary generation failed for DOI=%s: %s", doi, exc)

        valid_japanese = [
            candidate
            for candidate in japanese_candidates
            if not japanese_summary_issues(candidate, minimum, maximum)
        ]
        if valid_japanese:
            best = min(
                valid_japanese,
                key=lambda value: abs(summary_character_count(value) - target),
            )
            return SummaryResult(
                text=best,
                model=str(self.settings["model"]),
                character_count=summary_character_count(best),
                language="ja",
                used_fallback=False,
            )

        for candidate in reversed(english_candidates):
            if english_summary_is_valid(candidate):
                return SummaryResult(
                    text=candidate,
                    model=str(self.settings["model"]),
                    character_count=summary_character_count(candidate),
                    language="en",
                    used_fallback=True,
                )

        deterministic = extractive_english_fallback(
            abstract,
            int(self.settings.get("english_fallback_max_characters", 420)),
        )
        if deterministic:
            return SummaryResult(
                text=deterministic,
                model="extractive-abstract-fallback",
                character_count=summary_character_count(deterministic),
                language="en",
                used_fallback=True,
            )

        raise SummaryError("No Japanese, English, or extractive fallback summary was available.")
