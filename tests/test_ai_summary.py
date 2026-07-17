from paper_watch.ai_summary import (
    clean_summary,
    english_summary_is_valid,
    extract_structured_summaries,
    extractive_english_fallback,
    japanese_summary_issues,
)


def test_complete_japanese_summary_is_valid():
    text = (
        "N-heptyl-D-galactonamideとmolybdenumの可逆錯形成により、"
        "酸性条件で透明かつチキソトロピー性のmetallogelを形成し、"
        "周期的なmolybdate配列を実現した。"
    )
    assert japanese_summary_issues(text, 60, 150) == []


def test_truncated_japanese_summary_is_rejected():
    text = (
        "N-heptyl-D-galactonamideとmolybdenumの錯形成により、"
        "3.9または2.8 nm間隔の秩序"
    )
    issues = japanese_summary_issues(text, 40, 150)
    assert "does not end with Japanese full stop" in issues


def test_structured_summary_extraction():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"summary_ja":"macrocycleを合成した。","summary_en":"A macrocycle was synthesized and structurally characterized."}',
                    }
                ],
            }
        ]
    }
    ja, en = extract_structured_summaries(payload)
    assert ja == "macrocycleを合成した。"
    assert en.startswith("A macrocycle")


def test_english_summary_validation():
    text = (
        "The authors synthesized a new macrocycle, determined its crystal structure, "
        "and showed that it forms an ordered supramolecular assembly in the solid state."
    )
    assert english_summary_is_valid(text)


def test_extractive_fallback_uses_complete_sentences():
    abstract = (
        "The authors synthesized a macrocycle. "
        "Single-crystal X-ray diffraction revealed an ordered packing structure. "
        "Additional experiments were performed."
    )
    fallback = extractive_english_fallback(abstract, 180)
    assert fallback.endswith(".")
    assert "macrocycle" in fallback
    assert len(fallback) <= 180


def test_clean_summary_removes_wrapper():
    assert clean_summary("要約： macrocycleを合成した。") == "macrocycleを合成した。"
