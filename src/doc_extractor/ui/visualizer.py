"""
Visualizer Engine for Document Bounding Boxes & Grounding Overlays.
Renders 200 DPI canvas images with semi-transparent highlights, labels, and color-coded boxes.
"""

from typing import Any, Dict, List, Optional, Tuple, Union
import numpy as np
from PIL import Image, ImageDraw, ImageFont


COLOR_AUTO_APPROVED = (34, 197, 94)      # Emerald Green
COLOR_NEEDS_REVIEW = (239, 68, 68)       # Red / Coral
COLOR_MEDIUM_CONF = (234, 179, 8)       # Amber / Yellow
COLOR_ACTIVE_SELECTED = (59, 130, 246)   # Blue
COLOR_ALL_BLOCKS = (148, 163, 184)       # Slate Gray
COLOR_CHECKBOX = (168, 85, 247)          # Purple


def draw_bounding_boxes(
    image: Union[Image.Image, np.ndarray, bytes],
    boxes: List[Dict[str, Any]],
    selected_field_name: Optional[str] = None,
    show_all_detected_boxes: bool = False,
    all_blocks: Optional[List[Dict[str, Any]]] = None,
    draw_labels: bool = True,
) -> Image.Image:
    """
    Draws color-coded bounding boxes and optional semi-transparent overlays on a 200 DPI canvas image.
    
    Parameters:
    - image: PIL Image, numpy array, or raw bytes.
    - boxes: List of field box dicts, e.g.:
        {
            "field_name": "applicant.pan",
            "bbox": [x0, y0, x1, y1],
            "confidence": 0.95,
            "status": "passed" / "validation_failed",
            "review_required": False
        }
    - selected_field_name: Specific field to highlight boldly in Blue.
    - show_all_detected_boxes: Toggle to render all parsed OCR blocks / checkboxes.
    - all_blocks: List of raw layout blocks from Module B.
    """
    if isinstance(image, bytes):
        import io
        pil_img = Image.open(io.BytesIO(image)).convert("RGBA")
    elif isinstance(image, np.ndarray):
        pil_img = Image.fromarray(image).convert("RGBA")
    else:
        pil_img = image.convert("RGBA")

    # Create semi-transparent overlay layer
    overlay = Image.new("RGBA", pil_img.size, (255, 255, 255, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    draw_main = ImageDraw.Draw(pil_img)

    # 1. Optionally draw raw parsed OCR layout blocks (gray dashed / light border)
    if show_all_detected_boxes and all_blocks:
        for b in all_blocks:
            bbox = b.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            btype = b.get("block_type", "text_block")
            b_color = COLOR_CHECKBOX if "check" in btype else COLOR_ALL_BLOCKS
            # Light fill
            draw_overlay.rectangle(bbox, fill=(b_color[0], b_color[1], b_color[2], 30))
            # Border
            draw_main.rectangle(bbox, outline=(b_color[0], b_color[1], b_color[2], 160), width=1)

    # 2. Draw extracted schema field bounding boxes
    selected_box = None
    for item in boxes:
        fname = item.get("field_name", "")
        bbox = item.get("bbox")
        if not bbox or len(bbox) != 4:
            continue

        x0, y0, x1, y1 = [int(v) for v in bbox]
        if x1 <= x0 or y1 <= y0:
            continue

        is_selected = (fname == selected_field_name)
        if is_selected:
            selected_box = (fname, [x0, y0, x1, y1], item)
            continue  # Draw selected on top at the end

        conf = float(item.get("confidence", 1.0))
        rev_req = bool(item.get("review_required", False))

        if rev_req or conf < 0.75:
            color = COLOR_NEEDS_REVIEW
        elif conf < 0.85:
            color = COLOR_MEDIUM_CONF
        else:
            color = COLOR_AUTO_APPROVED

        # Semi-transparent fill
        draw_overlay.rectangle([x0, y0, x1, y1], fill=(color[0], color[1], color[2], 40))
        # Solid border
        draw_main.rectangle([x0, y0, x1, y1], outline=(color[0], color[1], color[2], 230), width=2)

        # Label tag
        if draw_labels:
            short_name = fname.split(".")[-1]
            draw_main.text((x0, max(0, y0 - 12)), f"{short_name} ({conf:.2f})", fill=(color[0], color[1], color[2], 255))

    # 3. Draw currently selected active field on top with vibrant blue highlight
    if selected_box:
        s_fname, s_bbox, s_item = selected_box
        x0, y0, x1, y1 = s_bbox
        # Stronger fill
        draw_overlay.rectangle([x0, y0, x1, y1], fill=(COLOR_ACTIVE_SELECTED[0], COLOR_ACTIVE_SELECTED[1], COLOR_ACTIVE_SELECTED[2], 85))
        # Bold border
        draw_main.rectangle([x0, y0, x1, y1], outline=(COLOR_ACTIVE_SELECTED[0], COLOR_ACTIVE_SELECTED[1], COLOR_ACTIVE_SELECTED[2], 255), width=4)

        if draw_labels:
            short_name = s_fname.split(".")[-1]
            conf = float(s_item.get("confidence", 1.0))
            # Text background badge
            draw_main.rectangle([x0, max(0, y0 - 18), x0 + len(short_name) * 8 + 60, y0], fill=(COLOR_ACTIVE_SELECTED[0], COLOR_ACTIVE_SELECTED[1], COLOR_ACTIVE_SELECTED[2], 230))
            draw_main.text((x0 + 4, max(0, y0 - 16)), f"{short_name} ({conf:.2f})", fill=(255, 255, 255, 255))

    # Composite alpha overlay
    final_img = Image.alpha_composite(pil_img, overlay).convert("RGB")
    return final_img

