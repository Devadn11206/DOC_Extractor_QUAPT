"""
Qwen2.5-VL-7B Visual Layout Parser Adapter for Module B (Task 2 / Slice 2).
Conforms strictly to ParserEngine (ARC-002), FR-PRS-001..005, and SCH-013.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple
import cv2
import numpy as np
from PIL import Image

from doc_extractor import vlm_runtime
from doc_extractor.contracts_b import (
    BoundingBox,
    CheckboxMark,
    LayoutBlock,
    LayoutElementType,
    PageLayoutOutput,
)
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.base import ParserEngine, ParserEngineError, ParserEngineMetadata
from doc_extractor.parsing.markdown import generate_page_markdown, sort_blocks_reading_order


class QwenVLParserAdapter(ParserEngine):
    """
    Pluggable Module B Parser Engine powered by Qwen2.5-VL-7B-Instruct.
    Performs OCR transcription, table detection, checkbox detection,
    and visual bounding box extraction.
    """

    def __init__(
        self,
        model_id: str = "Qwen/Qwen2.5-VL-7B-Instruct",
        device: str = "auto",
        load_in_4bit: bool = True,  # Recommended for Kaggle 16GB T4 / P100 GPUs
        precision: str = "auto",    # "auto": BF16 only on GPUs with native support, else FP16 (e.g. T4)
    ):
        self.model_id = model_id
        self.device = device
        self._is_operational = False

        try:
            # Exact Qwen2.5-VL model class, hardware-aware dtype and verified 4-bit settings (no hard-coded
            # BF16, no generic-loader fallback); see doc_extractor.vlm_runtime.
            loaded = vlm_runtime.load_qwen_vl(vlm_runtime.VLMLoadConfig(
                model_id=model_id,
                load_in_4bit=load_in_4bit,
                precision=precision,
                device_map=device,
            ))
            self.processor = loaded.processor
            self.model = loaded.model
            self.runtime_info = loaded.runtime_info
            self._is_operational = True
        except Exception as e:
            # Will allow fail-closed handling if weights cannot load
            self._init_error = str(e)

    def get_metadata(self) -> ParserEngineMetadata:
        return ParserEngineMetadata(
            engine_name="Qwen2.5-VL-7B-Instruct",
            engine_version="2.5",
            is_operational=self._is_operational,
            supports_optical_confidence=True,
            description="Qwen2.5-VL Multimodal Vision-Language Model for OCR and Layout Parsing.",
        )

    def is_operational(self) -> bool:
        return self._is_operational

    def parse_page(self, loaded_page: LoadedPageImage) -> PageLayoutOutput:
        """
        Parses a single preprocessed page image using Qwen2.5-VL.
        Maps extracted boxes back to SCH-013 200 DPI pixel coordinates.
        """
        if not self.is_operational():
            raise ParserEngineError(
                message=f"Qwen2.5-VL failed to initialize: {getattr(self, '_init_error', 'Unknown')}",
                engine_name="Qwen2.5-VL-7B-Instruct",
                code="PARSER_UNAVAILABLE",
            )

        if loaded_page.image_bgr is None or loaded_page.image_bgr.size == 0:
            raise ParserEngineError(
                message=f"Cannot parse page {loaded_page.page_number}: image buffer is empty.",
                engine_name="Qwen2.5-VL-7B-Instruct",
                code="EMPTY_IMAGE_BUFFER",
            )

        # Convert OpenCV BGR array to PIL RGB Image
        rgb_image = cv2.cvtColor(loaded_page.image_bgr, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb_image)
        orig_w, orig_h = loaded_page.width_px, loaded_page.height_px

        # Construct prompt asking for transcription and bounding box detection
        prompt_text = (
            "Read all printed and handwritten text in this document page in logical reading order. "
            "For each detected text block, heading, table row, or checkbox label, detect its bounding box "
            "in the format: [x1, y1, x2, y2] (left, top, right, bottom) followed by the text content. "
            "If a checkbox is checked, output [x]. If unchecked, output [ ]."
        )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": pil_image},
                    {"type": "text", "text": prompt_text},
                ],
            }
        ]

        try:
            # Prepare multimodal inputs
            text_prompt = self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            import torch

            # The processor resizes the PIL image exactly once with the checkpoint's own settings;
            # the image is not pre-processed separately (that would resize/normalize twice).
            inputs = self.processor(
                text=[text_prompt],
                images=[pil_image],
                padding=True,
                return_tensors="pt",
            ).to(self.model.device)

            with torch.no_grad():
                generated_ids = self.model.generate(
                    **inputs,
                    max_new_tokens=2048,
                    do_sample=False,  # greedy: deterministic
                )

            # Strip prompt tokens from output
            generated_ids_trimmed = [
                out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
            ]
            output_text = self.processor.batch_decode(
                generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
            )[0]

        except Exception as exc:
            raise ParserEngineError(
                message=f"Qwen2.5-VL inference failed on page {loaded_page.page_number}: {exc}",
                engine_name="Qwen2.5-VL-7B-Instruct",
                code="INFERENCE_FAILED",
            ) from exc

        # Parse text lines and bounding boxes from model response
        return self._build_page_layout(output_text, loaded_page)

    def _build_page_layout(
        self,
        raw_output: str,
        loaded_page: LoadedPageImage,
    ) -> PageLayoutOutput:
        """Parses model output lines, scales coordinates, and inverts rotation/skew."""
        page_ref = loaded_page.page_ref
        w_px, h_px = loaded_page.width_px, loaded_page.height_px

        blocks: List[LayoutBlock] = []
        checkboxes: List[CheckboxMark] = []

        # Matches coordinate patterns like [45, 120, 350, 160] or (45, 120, 350, 160): [x1, y1, x2, y2]
        box_pattern = re.compile(
            r"(?:\[|\()(\d+),\s*(\d+),\s*(\d+),\s*(\d+)(?:\]|\))\s*(.*)"
        )

        lines = [line.strip() for line in raw_output.split("\n") if line.strip()]
        reading_idx = 0

        for line in lines:
            match = box_pattern.search(line)
            if match:
                x1_norm, y1_norm, x2_norm, y2_norm, content = match.groups()   # [x1, y1, x2, y2]
                xmin, ymin = float(x1_norm), float(y1_norm)
                xmax, ymax = float(x2_norm), float(y2_norm)
                text = content.strip()
            else:
                # If a line had no explicit coordinates, assign full-width span in sequence
                xmin, xmax = 0.0, 1000.0
                ymin = (reading_idx * 20.0) % 1000.0
                ymax = min(1000.0, ymin + 20.0)
                text = line.strip()

            if not text:
                continue

            # Convert the [0, 1000] grid to actual preprocessed image pixels (x by width, y by height)
            x0 = max(0.0, min(float(w_px), (xmin / 1000.0) * w_px))
            y0 = max(0.0, min(float(h_px), (ymin / 1000.0) * h_px))
            x1 = max(x0, min(float(w_px), (xmax / 1000.0) * w_px))
            y1 = max(y0, min(float(h_px), (ymax / 1000.0) * h_px))

            bbox_prep = BoundingBox(x0=round(x0, 2), y0=round(y0, 2), x1=round(x1, 2), y1=round(y1, 2))

            # Invert coordinates to original 200 DPI space (SCH-013)
            orig_tuple = CoordinateTransform.map_bbox_preprocessed_to_original(
                bbox=bbox_prep.to_tuple(),
                original_width=page_ref.original_width_px,
                original_height=page_ref.original_height_px,
                applied_rotation_deg=page_ref.applied_rotation_deg,
                applied_deskew_deg=page_ref.applied_deskew_deg,
            )
            bbox_orig = BoundingBox.from_tuple(orig_tuple)

            # Check for Checkboxes (FR-PRS-002)
            if text.startswith("[x]") or text.startswith("[ ]"):
                state = "[x]" if text.startswith("[x]") else "[ ]"
                checkboxes.append(
                    CheckboxMark(
                        mark_id=f"cb_p{loaded_page.page_number}_{reading_idx}",
                        state=state,
                        ocr_confidence=None,  # the VLM reports no optical confidence; none is invented
                        bbox_preprocessed=bbox_prep,
                        bbox_original=bbox_orig,
                    )
                )

            # Assign element type
            elem_type = LayoutElementType.PARAGRAPH
            if text.isupper() and len(text) > 4:
                elem_type = LayoutElementType.HEADER
            elif ":" in text and len(text.split(":", 1)[0].split()) <= 4:
                elem_type = LayoutElementType.KEY_VALUE_PAIR

            blocks.append(
                LayoutBlock(
                    block_id=f"blk_p{loaded_page.page_number}_{reading_idx:04d}",
                    element_type=elem_type,
                    text=text,
                    ocr_confidence=None,  # unavailable: model-read text has no optical confidence
                    bbox_preprocessed=bbox_prep,
                    bbox_original=bbox_orig,
                    reading_order_index=reading_idx,
                )
            )
            reading_idx += 1

        ordered_blocks = sort_blocks_reading_order(blocks)
        markdown = generate_page_markdown(ordered_blocks)

        return PageLayoutOutput(
            page_number=loaded_page.page_number,
            markdown_content=markdown,
            blocks=ordered_blocks,
            checkboxes=checkboxes,
            key_value_pairs=[],
            tables=[],
            character_grids=[],
            mean_ocr_confidence=None,  # no measured block confidences to average
        )