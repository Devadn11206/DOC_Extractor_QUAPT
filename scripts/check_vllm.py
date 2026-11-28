#!/usr/bin/env python3
"""
End-to-end check of the vLLM servers using the engine's own code and a synthetic page (no real data).

    python scripts/check_vllm.py

Reads VLLM_API_BASE, VLLM_TEXT_API_BASE, VLLM_OCR_API_BASE and VLLM_API_KEY. Exit code 0 only if every
configured model answers correctly.
"""

import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from doc_extractor import vllm_client  # noqa: E402
from doc_extractor.adapters import DEFAULT_OCR_MODEL, DEFAULT_TEXT_MODEL, OCRTextAdapter  # noqa: E402
from doc_extractor.extractor import SchemaGuidedExtractor  # noqa: E402
from doc_extractor.routing import DEFAULT_MODEL  # noqa: E402

NAME = "ASHA TESTPERSON"
PROMPT = 'Return only JSON: {"applicant_name": {"value": "<name or null>", "bbox": [x1, y1, x2, y2]}}'


def synthetic_page(path: str) -> None:
    image = Image.new("RGB", (900, 400), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=36)
    except TypeError:                       # Pillow < 10.1 has one small bitmap font
        font = ImageFont.load_default()
    draw.text((40, 60), "ACCOUNT OPENING FORM (SYNTHETIC)", fill="black", font=font)
    draw.text((40, 180), f"Applicant Name: {NAME}", fill="black", font=font)
    image.save(path)


def step(label: str, action) -> bool:
    started = time.perf_counter()
    try:
        detail = action()
    except Exception as exc:
        print(f"FAIL  {label}: {type(exc).__name__}: {exc}")
        return False
    print(f"OK    {label} ({time.perf_counter() - started:.1f} s) {detail}")
    return True


def main() -> int:
    configured = {role: vllm_client.endpoint(role) for role in vllm_client.ROLE_ENV}
    for role, url in configured.items():
        print(f"{vllm_client.ROLE_ENV[role]:<20} {url or '(not set: skipped)'}")
    if not any(configured.values()):
        print("Nothing to check: set at least one URL.")
        return 2

    results = []
    with tempfile.TemporaryDirectory() as folder:
        page = os.path.join(folder, "page.png")
        synthetic_page(page)
        adapter = OCRTextAdapter()

        def confidence_of(field: dict) -> float:
            if field.get("confidence") is None:
                raise ValueError("no confidence score: the server's token probabilities could not be used")
            return field["confidence"]

        def expect_name(text: str) -> str:
            if NAME.split()[0] not in text.upper():
                raise ValueError(f"expected the applicant name, got {text[:200]!r}")
            return ""

        if configured["vision"]:
            extractor = SchemaGuidedExtractor(model_name=DEFAULT_MODEL, use_llm=True, allow_fallback=False)

            def read() -> str:
                raw, _ = extractor.extract_page_image(page, None, system_prompt=PROMPT)
                field = raw.get("applicant_name") or {}
                expect_name(str(field.get("value")))
                return f"confidence={confidence_of(field)} bbox={field.get('bbox')}"

            results.append(step(f"vision extraction [{DEFAULT_MODEL}]", read))
            results.append(step("vision classification",
                                lambda: f"category={extractor.classify_page_image(page).get('category')}"))
        if configured["ocr"]:
            results.append(step(f"OCR [{DEFAULT_OCR_MODEL}]", lambda: expect_name(adapter.read_text(page))))
        if configured["text"]:
            def to_fields() -> str:
                raw = adapter.text_to_fields(f"Applicant Name: {NAME}", PROMPT)
                field = raw.get("applicant_name") or {}
                expect_name(str(field.get("value")))
                return f"confidence={confidence_of(field)}"

            results.append(step(f"text to JSON [{DEFAULT_TEXT_MODEL}]", to_fields))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
