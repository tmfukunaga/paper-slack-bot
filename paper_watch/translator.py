from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def _load_model(model_name: str):
    # Imported lazily so scoring tests can run without loading the model.
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
    model.eval()
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    return tokenizer, model


def _sentence_units(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    # Keep the punctuation with each sentence. Abbreviation handling is not
    # perfect, but token-based regrouping below prevents oversized inputs.
    units = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])", text)
    return [unit.strip() for unit in units if unit.strip()]


def _split_by_tokens(text: str, tokenizer, max_tokens: int) -> list[str]:
    token_ids = tokenizer.encode(text, add_special_tokens=False)
    if len(token_ids) <= max_tokens:
        return [text]

    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join([*current, word])
        if current and len(tokenizer.encode(trial, add_special_tokens=False)) > max_tokens:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks


def _make_chunks(text: str, tokenizer, max_tokens: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in _sentence_units(text):
        for part in _split_by_tokens(unit, tokenizer, max_tokens):
            trial = f"{current} {part}".strip()
            if current and len(tokenizer.encode(trial, add_special_tokens=False)) > max_tokens:
                chunks.append(current)
                current = part
            else:
                current = trial
    if current:
        chunks.append(current)
    return chunks


def translate_abstract(text: str, config: dict) -> str:
    """Translate an English abstract to Japanese with a local open model.

    No paid translation API is called. The model is downloaded from Hugging
    Face on the first run and is cached by GitHub Actions for later runs.
    """
    if not text:
        return "取得できませんでした。"

    translation_cfg = config.get("translation", {})
    model_name = translation_cfg.get("model", "Helsinki-NLP/opus-mt-en-jap")
    max_tokens = int(translation_cfg.get("max_input_tokens", 400))
    batch_size = int(translation_cfg.get("batch_size", 8))
    num_beams = int(translation_cfg.get("num_beams", 4))

    try:
        import torch

        tokenizer, model = _load_model(model_name)
        chunks = _make_chunks(text, tokenizer, max_tokens)
        translated: list[str] = []

        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            encoded = tokenizer(
                batch,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=max_tokens + 16,
            )
            with torch.inference_mode():
                outputs = model.generate(
                    **encoded,
                    num_beams=num_beams,
                    max_new_tokens=512,
                    early_stopping=True,
                )
            translated.extend(
                tokenizer.batch_decode(outputs, skip_special_tokens=True)
            )

        result = "".join(piece.strip() for piece in translated if piece.strip())
        return result or "翻訳結果を取得できませんでした。"
    except Exception as exc:
        LOGGER.exception("Local abstract translation failed: %s", exc)
        return "翻訳に失敗しました。原文abstractを確認してください。"
