"""
Bounding-box contract (PRD v1.5 FR-EXT-004 / GR-002 / FR-EXT-011).

One canonical internal form: model boxes are ``[x1, y1, x2, y2]`` (left, top, right, bottom) on a 0-1000
grid relative to the image the model saw; evidence boxes are ``(x0, y0, x1, y1)`` pixels of the 200-DPI
original page. These tests pin the ORIENTATION (X is never swapped with Y) at every stage:
prompt -> grounding -> CoordinateTransform -> evidence -> rendering.
"""

from types import SimpleNamespace as NS

from PIL import Image, ImageChops
import pytest

from doc_extractor import app
from doc_extractor import vlm_grounding as vg
from doc_extractor import vlm_runtime as rt
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.routing import DocumentCategory, build_generic_prompt
from doc_extractor.schemas import CASASchema


def _ref(width, height, rotation=0, deskew=0.0):
    page = NS(preprocessed_width_px=width, preprocessed_height_px=height, original_width_px=width,
              original_height_px=height, applied_rotation_deg=rotation, applied_deskew_deg=deskew)
    return NS(page_ref=page, page_number=1)


# -- the contract at the grounding stage -----------------------------------------------------------

def test_a_wide_off_centre_box_maps_to_the_right_pixels_not_the_swapped_ones():
    """Regression: the PR's grounding read [ymin, xmin, ymax, xmax], so a v1.5 box came out transposed."""
    width, height = 2000, 1000                       # landscape page: a swap cannot hide
    box = (100.0, 50.0, 300.0, 90.0)                 # x 100..300, y 50..90 on 0-1000  (wide, near the top)
    item = NS(bbox_norm=vg._valid_bbox(list(box)), page_number=1, raw_value="X", uncertain=False)

    x0, y0, x1, y1 = vg._hint_for(item, {1: _ref(width, height)})["bbox"]

    assert (x0, y0, x1, y1) == pytest.approx((200.0, 50.0, 600.0, 90.0))
    assert (x1 - x0) > (y1 - y0)                     # still wide, not tall


@pytest.mark.parametrize("box", [(10, 20, 30, 40), (500, 100, 900, 150), (0, 0, 1000, 1000), (333, 666, 444, 777)])
@pytest.mark.parametrize("size", [(1000, 1000), (1654, 2339), (2339, 1654), (400, 1200)])
def test_x_is_scaled_by_width_and_y_by_height_for_any_page_shape(box, size):
    width, height = size
    item = NS(bbox_norm=vg._valid_bbox(list(box)), page_number=1, raw_value="v", uncertain=False)
    x0, y0, x1, y1 = vg._hint_for(item, {1: _ref(width, height)})["bbox"]
    assert (x0, y0, x1, y1) == pytest.approx(           # CoordinateTransform rounds to 2 decimals
        (box[0] * width / 1000, box[1] * height / 1000, box[2] * width / 1000, box[3] * height / 1000), abs=0.01
    )


def test_valid_bbox_accepts_xyxy_orders_swapped_corners_and_rejects_the_rest():
    assert vg._valid_bbox([100, 50, 300, 90]) == (100.0, 50.0, 300.0, 90.0)
    assert vg._valid_bbox([300, 90, 100, 50]) == (100.0, 50.0, 300.0, 90.0)      # same rectangle, corners reordered
    assert vg._valid_bbox([100, 50, 100, 90]) is None                              # no area
    assert vg._valid_bbox([100, 50, 300, 1500]) is None                            # raw pixels, not the 0-1000 grid
    assert vg._valid_bbox([-1, 0, 10, 10]) is None
    assert vg._valid_bbox(["a", 0, 10, 10]) is None
    assert vg._valid_bbox([1, 2, 3]) is None and vg._valid_bbox(None) is None


# -- pages turned upright before the model reads them ----------------------------------------------

@pytest.mark.parametrize("clockwise", [90, 180, 270])
def test_unrotate_maps_a_box_on_the_upright_image_back_to_the_stored_page(clockwise):
    """Checked against PIL itself: draw a rectangle, turn the page clockwise, find it, map it back."""
    width, height = 1000, 500
    stored = Image.new("L", (width, height), 255)
    rect = (120, 60, 520, 210)                                   # pixels on the STORED page
    stored.paste(0, rect)
    upright = stored.rotate(-clockwise, expand=True)             # PIL: negative angle = clockwise
    found = ImageChops.invert(upright).getbbox()                 # rectangle on the upright image
    uw, uh = upright.size
    seen_norm = (found[0] * 1000 / uw, found[1] * 1000 / uh, found[2] * 1000 / uw, found[3] * 1000 / uh)

    back = vg.unrotate_bbox_norm(seen_norm, clockwise)

    expected = (rect[0] * 1000 / width, rect[1] * 1000 / height, rect[2] * 1000 / width, rect[3] * 1000 / height)
    assert back == pytest.approx(expected, abs=3.0)


def test_unrotate_is_identity_at_zero_and_rejects_other_angles():
    assert vg.unrotate_bbox_norm((1, 2, 3, 4), 0) == (1, 2, 3, 4)
    with pytest.raises(ValueError):
        vg.unrotate_bbox_norm((1, 2, 3, 4), 45)


# -- prompts, families, rendering ------------------------------------------------------------------

def test_every_prompt_asks_for_xyxy_and_none_for_the_old_y_first_order():
    prompts = [SchemaGuidedExtractor(use_llm=False)._build_system_prompt(CASASchema)]
    prompts += [build_generic_prompt(c) for c in DocumentCategory]
    for prompt in prompts:
        assert "[x1, y1, x2, y2]" in prompt
        assert "ymin" not in prompt and "[y1, x1" not in prompt


def test_the_qwen_vl_parser_adapter_prompt_and_parser_use_xyxy():
    import inspect
    from doc_extractor.parsing import qwen_vl_adapter

    source = inspect.getsource(qwen_vl_adapter)
    assert "[x1, y1, x2, y2]" in source and "[ymin, xmin, ymax, xmax]" not in source


def test_each_supported_family_declares_its_convention_and_unverified_ones_say_so():
    for family in (rt.FAMILY_QWEN3_VL, rt.FAMILY_QWEN2_5_VL):
        assert family.bbox_convention["order"] == "xyxy" and family.bbox_convention["grid"] == 1000
    assert rt.FAMILY_QWEN3_VL.bbox_verified is True
    assert rt.FAMILY_QWEN2_5_VL.bbox_verified is False           # taken from the PRD, not confirmed on the model


def test_drawing_uses_the_evidence_pixels_as_given_without_swapping_axes():
    canvas = Image.new("RGB", (400, 200), "white")
    field = {"value": "X", "evidence": {"page": 1, "bbox": (20, 40, 120, 90)}}

    out = app.draw_bounding_boxes(canvas, {"f": field}, page=1)

    assert out.getpixel((21, 60)) != (255, 255, 255)             # left edge of (x0=20, y 40..90)
    assert out.getpixel((70, 41)) != (255, 255, 255)             # top edge
    assert out.getpixel((41, 100)) == (255, 255, 255)            # where a transposed rectangle's edge would be
    assert out.getpixel((60, 21)) == (255, 255, 255)


def test_drawing_filters_evidence_by_page_and_includes_additional_evidence():
    canvas = Image.new("RGB", (300, 300), "white")
    field = {"value": "X",
             "evidence": {"page": 1, "bbox": (10, 10, 60, 40)},
             "additional_evidence": [{"page": 2, "bbox": (100, 100, 200, 160)}]}
    page1 = app.draw_bounding_boxes(canvas, {"f": field}, page=1)
    page2 = app.draw_bounding_boxes(canvas, {"f": field}, page=2)
    assert page1.getpixel((11, 20)) != (255, 255, 255) and page1.getpixel((101, 120)) == (255, 255, 255)
    assert page2.getpixel((101, 120)) != (255, 255, 255) and page2.getpixel((11, 20)) == (255, 255, 255)
