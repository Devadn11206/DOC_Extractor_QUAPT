"""
v1.5 Qwen runtime operating defaults (T4 / Kaggle): image caps, model-aware VRAM, SDPA, single-device map,
dtype rule, trust_remote_code allowlist. Everything is mocked: no GPU, torch or model download.
No claim about real-GPU behaviour is made or implied by these tests.
"""

import sys
import types

from PIL import Image
import pytest

from doc_extractor import extractor as ex
from doc_extractor import vlm_runtime as rt
from doc_extractor.extractor import SchemaGuidedExtractor

from tests.test_qwen3_vl_runtime_readiness import (  # noqa: F401  (fakes shared with the readiness suite)
    GPU_2xT4,
    GPU_A100,
    GPU_SMALL,
    GPU_T4,
    FakeTorch,
    extractor_with_fake_model,
    install_fake_utils,
    load,
)


# -- pixel caps -----------------------------------------------------------------------------------

def test_default_caps_are_1_3_mp_for_extraction_and_0_5_mp_for_classification():
    assert rt.DEFAULT_MAX_IMAGE_PIXELS == 1_300_000 and rt.DEFAULT_CLASSIFY_MAX_PIXELS == 500_000
    assert ex.MAX_IMAGE_PIXELS == rt.DEFAULT_MAX_IMAGE_PIXELS
    assert ex.CLASSIFY_MAX_PIXELS == rt.DEFAULT_CLASSIFY_MAX_PIXELS
    assert SchemaGuidedExtractor(use_llm=False).max_pixels == 1_300_000
    assert SchemaGuidedExtractor(use_llm=False, max_pixels=4_200_000).max_pixels == 4_200_000   # explicit override


@pytest.mark.parametrize("raw,expected", [(None, 7), ("", 7), ("1000000", 1_000_000), ("abc", 7), ("0", 7), ("-5", 7)])
def test_env_int_uses_a_valid_positive_integer_else_the_default(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("DOC_TEST_CAP", raising=False)
    else:
        monkeypatch.setenv("DOC_TEST_CAP", raw)
    assert rt._env_int("DOC_TEST_CAP", 7) == expected


def test_a_200_dpi_a4_page_is_capped_to_1_3_mp_keeping_its_aspect_ratio_and_the_patch_factor():
    cfg = rt.build_image_config(rt.FAMILY_QWEN3_VL, None, rt.DEFAULT_MAX_IMAGE_PIXELS)
    h, w = rt.expected_model_input_size(2339, 1654, cfg)             # A4 @ 200 DPI = 3.87 MP
    assert h * w <= 1_300_000 and h % 32 == 0 and w % 32 == 0
    assert abs((h / w) - (2339 / 1654)) < 0.05          # only rounding to the 32-px factor
    assert rt.estimate_visual_tokens(2339, 1654, cfg) < 1400          # far below the ~3.8k tokens of the full page


def test_the_extraction_call_sends_the_1_3_mp_budget_and_classification_the_smaller_one(monkeypatch, tmp_path):
    calls, _ = install_fake_utils(monkeypatch)
    ext = extractor_with_fake_model()
    page = tmp_path / "p.png"
    Image.new("RGB", (100, 100), "white").save(page)

    ext._generate_from_image(page, "S", "U", 8)
    ext._generate_from_image(page, "S", "U", 8, max_pixels=ex.CLASSIFY_MAX_PIXELS)

    extraction, classification = (c["messages"][1]["content"][0] for c in calls)
    assert extraction["max_pixels"] == 1_300_000
    assert classification["max_pixels"] == 500_000 and classification["min_pixels"] == 65536


def test_classify_page_image_uses_the_classification_cap(monkeypatch, tmp_path):
    calls, _ = install_fake_utils(monkeypatch)
    ext = extractor_with_fake_model()
    ext._processor.batch_decode = lambda *a, **k: ['{"category": "structured"}']
    page = tmp_path / "p.png"
    Image.new("RGB", (64, 64), "white").save(page)
    assert ext.classify_page_image(page)["category"] == "structured"
    assert calls[0]["messages"][1]["content"][0]["max_pixels"] == 500_000


def test_gpu_memory_is_released_after_every_generation(monkeypatch, tmp_path):
    install_fake_utils(monkeypatch)
    torch = sys.modules["torch"]
    ext = extractor_with_fake_model()
    page = tmp_path / "p.png"
    Image.new("RGB", (32, 32), "white").save(page)
    assert torch.cuda.cache_clears == []
    assert ext._generate_from_image(page, "S", "U", 4)
    assert len(torch.cuda.cache_clears) == 1                    # torch.cuda.empty_cache() ran after the generation
    ext._generate_from_image(page, "S", "U", 4)
    assert len(torch.cuda.cache_clears) == 2


def test_the_cap_is_never_lowered_silently_after_a_failure(monkeypatch, tmp_path):
    """An OOM surfaces as an error; max_pixels is only ever what the caller or the environment set."""
    install_fake_utils(monkeypatch)
    ext = extractor_with_fake_model()
    ext._model.generate = lambda **k: (_ for _ in ()).throw(RuntimeError("CUDA out of memory"))
    page = tmp_path / "p.png"
    Image.new("RGB", (32, 32), "white").save(page)
    with pytest.raises(RuntimeError, match="out of memory"):
        ext._generate_from_image(page, "S", "U", 4)
    assert ext.max_pixels == 1_300_000


# -- loader settings -------------------------------------------------------------------------------

def test_loader_uses_sdpa_and_a_single_device_by_default():
    _, tf = load(GPU_2xT4)
    kwargs = tf.Qwen3VLForConditionalGeneration.calls[0]
    assert kwargs["attn_implementation"] == "sdpa"
    assert kwargs["device_map"] == "cuda:0"                       # DOC_DEVICE_MAP default "single" -> GPU 0 only


@pytest.mark.parametrize("device_map,expected", [("balanced", "balanced"), ("auto", "auto"), ("cuda:1", "cuda:1")])
def test_other_device_maps_are_passed_through_unchanged(device_map, expected):
    _, tf = load(GPU_2xT4, device_map=device_map)
    assert tf.Qwen3VLForConditionalGeneration.calls[0]["device_map"] == expected


def test_single_resolves_to_auto_without_a_gpu_and_runtime_info_records_the_settings():
    loaded, tf = load([], allow_cpu=True, load_in_4bit=False)
    assert tf.Qwen3VLForConditionalGeneration.calls[0]["device_map"] == "auto"
    info = loaded.runtime_info
    assert info["attn_implementation"] == "sdpa" and info["device_map"] == "auto"
    assert info["bbox_convention"] == {"order": "xyxy", "grid": 1000, "verified": True}
    assert info["trust_remote_code"] is False


def test_dtype_rule_is_shared_between_the_loader_and_gpu_dtype(monkeypatch):
    for gpus, expected in ((GPU_T4, "DTYPE_float16"), (GPU_2xT4, "DTYPE_float16"), (GPU_A100, "DTYPE_bfloat16"),
                           ([], "DTYPE_float32")):
        monkeypatch.setitem(sys.modules, "torch", FakeTorch(gpus))
        assert ex.gpu_dtype() == expected
    loaded, _ = load(GPU_T4)
    assert loaded.runtime_info["dtype"] == "float16" and "float16" in str(loaded.runtime_info["quantization"])


# -- model-aware VRAM ------------------------------------------------------------------------------

@pytest.mark.parametrize("model,quantized,expected", [
    ("Qwen/Qwen3-VL-8B-Instruct", False, 16.0), ("Qwen/Qwen3-VL-8B-Instruct", True, 6.0),   # PRD NFR-007 figures
    ("Qwen/Qwen3-VL-4B-Instruct", False, 8.0), ("Qwen/Qwen3-VL-4B-Instruct", True, 4.0),
    ("Qwen/Qwen2.5-VL-7B-Instruct", False, 14.0), ("Qwen/Qwen3-VL-2B-Instruct", True, 3.0),
    ("local/checkpoint-dir", False, 16.0), ("local/checkpoint-dir", True, 6.0),               # unknown size: conservative
])
def test_vram_requirement_scales_with_the_checkpoint_size(model, quantized, expected):
    assert rt.estimate_min_vram_gib(model, quantized) == pytest.approx(expected)


def test_a_4b_model_fits_a_t4_unquantized_but_the_8b_does_not():
    load(GPU_T4, load_in_4bit=False)                                              # 8 GiB <= 14.6
    with pytest.raises(rt.VLMRuntimeError) as exc:
        load(GPU_T4, model_id=rt.QWEN3_VL_8B_MODEL_ID, load_in_4bit=False)         # 16 GiB > 14.6
    assert exc.value.code == "VLM_INSUFFICIENT_VRAM"


def test_a_single_device_map_counts_only_that_gpu_while_auto_sums_them():
    with pytest.raises(rt.VLMRuntimeError):
        load(GPU_2xT4, model_id=rt.QWEN3_VL_8B_MODEL_ID, load_in_4bit=False, precision="float16")   # GPU 0 only: 14.6
    load(GPU_2xT4, model_id=rt.QWEN3_VL_8B_MODEL_ID, load_in_4bit=False, precision="float16",
         device_map="auto")                                                                         # 29.2 across both


# -- trust_remote_code allowlist -------------------------------------------------------------------

def test_remote_code_is_never_allowed_for_qwen_and_only_the_documented_exception_is_allowlisted(monkeypatch):
    monkeypatch.delenv(rt.TRUST_REMOTE_CODE_ENV, raising=False)
    for model in (rt.QWEN3_VL_4B_MODEL_ID, rt.QWEN3_VL_8B_MODEL_ID, rt.QWEN2_5_VL_MODEL_ID,
                  "Qwen/Qwen3-4B-Instruct-2507", "Qwen/Qwen2.5-7B-Instruct"):
        assert rt.resolve_trust_remote_code(model) is False
    assert set(rt.TRUST_REMOTE_CODE_ALLOWLIST) == {"zai-org/GLM-OCR"}
    assert "UNVERIFIED" in rt.TRUST_REMOTE_CODE_ALLOWLIST["zai-org/GLM-OCR"]       # the exception is documented
    assert rt.resolve_trust_remote_code("zai-org/GLM-OCR") is True


def test_explicitly_requesting_remote_code_for_a_non_allowlisted_model_is_refused(monkeypatch):
    monkeypatch.delenv(rt.TRUST_REMOTE_CODE_ENV, raising=False)
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.resolve_trust_remote_code(rt.QWEN3_VL_4B_MODEL_ID, requested=True)
    assert exc.value.code == "VLM_REMOTE_CODE_NOT_ALLOWED"
    with pytest.raises(rt.VLMRuntimeError):
        load(GPU_T4, trust_remote_code=True)                      # the loader enforces it too


def test_an_explicit_environment_opt_in_allows_exactly_the_named_models(monkeypatch):
    monkeypatch.setenv(rt.TRUST_REMOTE_CODE_ENV, "acme/special-ocr, acme/other")
    assert rt.resolve_trust_remote_code("acme/special-ocr") is True
    assert rt.resolve_trust_remote_code("acme/unlisted") is False


def test_the_loader_passes_trust_remote_code_false_for_qwen():
    loaded, tf = load(GPU_T4)
    assert tf.Qwen3VLForConditionalGeneration.calls[0]["trust_remote_code"] is False
    assert tf.processor_box.loaded[0]["trust_remote_code"] is False


def test_no_source_file_hard_codes_trust_remote_code_true():
    import ast
    import pathlib
    root = pathlib.Path(rt.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "trust_remote_code" \
                    and isinstance(node.value, ast.Constant) and node.value.value is True:
                offenders.append(f"{path.relative_to(root)}:{node.value.lineno}")
    assert offenders == []
