"""
Phase 1 / Task 8B: Qwen3-VL runtime readiness and hardware compatibility.

Deterministic tests with fakes only: no model weights, no GPU, no PyTorch/Transformers install is
needed (fake ``torch`` / ``transformers`` / ``qwen_vl_utils`` modules). They pin down:

* the checkpoint id and the exact Transformers class (never a generic text-only loader);
* actionable errors when the stack is incompatible (missing class, old Transformers, unsupported
  checkpoint, missing bitsandbytes/accelerate, no CUDA, too little VRAM, load failures);
* hardware-aware precision (BF16 only with native support; FP16 on e.g. a Tesla T4) and a 4-bit
  configuration whose compute dtype is the selected dtype;
* the image path (image_patch_size=16, explicit min/max pixels, no double resize);
* bounding-box compatibility with the Module A geometry and CoordinateTransform;
* unchanged mocked-extraction behaviour (confidence/review policy untouched).
"""

import importlib
import io
import sys
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest
from PIL import Image

from doc_extractor import vlm_runtime as rt
from doc_extractor.contracts_b import ModuleAPageRef
from doc_extractor.contracts_c import FieldGrounding, PageImageRef
from doc_extractor.exceptions import LLMExecutionError
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.vlm_grounding import grounding_to_evidence_hints

from tests.test_canonical_module_c_orchestration import (  # noqa: F401  (fixtures + helpers)
    _isolate_environment,
    temp_storage,
)
from tests.test_multipage_extraction import PageAwareVLM, _run, _vlm_pipeline

GiB = 1024 ** 3

# --------------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------------

GPU_T4 = [("Tesla T4", (7, 5), 14.6)]
GPU_2xT4 = [("Tesla T4", (7, 5), 14.6), ("Tesla T4", (7, 5), 14.6)]
GPU_A100 = [("NVIDIA A100-SXM4-40GB", (8, 0), 39.5)]
GPU_L4 = [("NVIDIA L4", (8, 9), 22.0)]
GPU_V100 = [("Tesla V100", (7, 0), 15.7)]
GPU_SMALL = [("Tiny GPU", (7, 5), 4.0)]


class FakeTorch(types.SimpleNamespace):
    """Just enough ``torch`` for hardware detection and dtype objects."""

    def __init__(self, gpus: Optional[List[Tuple[str, Tuple[int, int], float]]] = None):
        gpus = gpus or []
        props = [
            types.SimpleNamespace(name=n, major=c[0], minor=c[1], total_memory=int(mem * GiB))
            for n, c, mem in gpus
        ]
        cuda = types.SimpleNamespace(
            is_available=lambda: bool(props),
            device_count=lambda: len(props),
            get_device_properties=lambda i: props[i],
            is_bf16_supported=lambda *a, **k: True,   # real PyTorch says True on a T4 by emulation: must be ignored
        )
        super().__init__(
            cuda=cuda,
            bfloat16="DTYPE_bfloat16",
            float16="DTYPE_float16",
            float32="DTYPE_float32",
            no_grad=_NoGrad,
        )


class _NoGrad:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeBitsAndBytesConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeModel:
    """Records from_pretrained kwargs (the class is looked up by the loader on the transformers module)."""

    calls: List[Dict[str, Any]] = []
    fail_with: Optional[Exception] = None
    device = "cuda:0"

    def __init__(self, source: str, kwargs: Dict[str, Any]):
        self.source, self.kwargs, self.evaluated = source, kwargs, False

    @classmethod
    def from_pretrained(cls, source, **kwargs):
        if cls.fail_with is not None:
            raise cls.fail_with
        cls.calls.append({"source": source, **kwargs})
        return cls(source, kwargs)

    def eval(self):
        self.evaluated = True
        return self


def make_model_class(name: str):
    return type(name, (FakeModel,), {"calls": [], "fail_with": None})


class FakeProcessorBox:
    def __init__(self, patch_size: Optional[int] = 16):
        self.patch_size = patch_size
        self.loaded: List[Dict[str, Any]] = []

    def from_pretrained(self, source, **kwargs):
        self.loaded.append({"source": source, **kwargs})
        proc = types.SimpleNamespace(source=source)
        if self.patch_size is not None:
            proc.image_processor = types.SimpleNamespace(patch_size=self.patch_size)
        return proc


class TrapNamespace(types.SimpleNamespace):
    """Fails the test if a generic (text-only) loader is even looked up."""

    FORBIDDEN = ("AutoModelForCausalLM", "AutoModel", "AutoModelForImageTextToText", "AutoModelForMultimodalLM")

    def __getattribute__(self, item):
        if item in TrapNamespace.FORBIDDEN:
            raise AssertionError(f"generic loader {item} must never be used")
        return super().__getattribute__(item)


def make_transformers(version="4.57.1", with_qwen3=True, with_qwen25=True, patch_size=16, with_bnb=True):
    box = FakeProcessorBox(patch_size)
    ns = TrapNamespace(
        __version__=version,
        AutoProcessor=box,
    )
    if with_qwen3:
        ns.Qwen3VLForConditionalGeneration = make_model_class("Qwen3VLForConditionalGeneration")
    if with_qwen25:
        ns.Qwen2_5_VLForConditionalGeneration = make_model_class("Qwen2_5_VLForConditionalGeneration")
    if with_bnb:
        ns.BitsAndBytesConfig = FakeBitsAndBytesConfig
    ns.processor_box = box
    return ns


def lookup_factory(**versions):
    import importlib.metadata as md

    table = {"transformers": "4.57.1", "torch": "2.6.0", "torchvision": "0.21.0", "qwen-vl-utils": "0.0.14",
             "accelerate": "1.10.0", "bitsandbytes": "0.47.0"}
    table.update({k.replace("_", "-"): v for k, v in versions.items()})

    def lookup(name):
        value = table.get(name)
        if value is None:
            raise md.PackageNotFoundError(name)
        return value

    return lookup


def load(gpus, config=None, transformers=None, lookup=None, **config_kwargs):
    cfg = config or rt.VLMLoadConfig(**config_kwargs)
    tf = transformers or make_transformers()
    return rt.load_qwen_vl(cfg, torch_module=FakeTorch(gpus), transformers_module=tf,
                           version_lookup=lookup or lookup_factory()), tf


# ============================================================================
# 1. Model identifier
# ============================================================================

def test_primary_checkpoint_is_qwen3_vl_8b_instruct_everywhere():
    assert rt.QWEN3_VL_MODEL_ID == "Qwen/Qwen3-VL-8B-Instruct"
    assert rt.VLMLoadConfig().model_id == rt.QWEN3_VL_MODEL_ID
    assert SchemaGuidedExtractor().model_name == "Qwen/Qwen3-VL-8B-Instruct"
    assert rt.resolve_family(rt.QWEN3_VL_MODEL_ID) is rt.FAMILY_QWEN3_VL


def test_loaded_model_id_is_passed_to_the_loader_and_recorded():
    loaded, tf = load(GPU_T4)
    assert tf.Qwen3VLForConditionalGeneration.calls[0]["source"] == "Qwen/Qwen3-VL-8B-Instruct"
    assert tf.processor_box.loaded[0]["source"] == "Qwen/Qwen3-VL-8B-Instruct"
    assert loaded.runtime_info["model_id"] == "Qwen/Qwen3-VL-8B-Instruct"


# ============================================================================
# 2-4. Model-class selection, missing compatibility, unsupported checkpoints
# ============================================================================

def test_qwen3_vl_uses_the_exact_conditional_generation_class_and_no_generic_loader():
    loaded, tf = load(GPU_T4)            # TrapNamespace fails the test on any AutoModel* lookup
    assert len(tf.Qwen3VLForConditionalGeneration.calls) == 1
    assert tf.Qwen2_5_VLForConditionalGeneration.calls == []
    assert loaded.runtime_info["loader_class"] == "transformers.Qwen3VLForConditionalGeneration"
    assert loaded.model.evaluated is True


def test_qwen2_5_vl_alternate_uses_its_own_class_with_its_own_patch_size():
    loaded, tf = load(GPU_T4, transformers=make_transformers(version="4.57.1", patch_size=14),
                      model_id="Qwen/Qwen2.5-VL-7B-Instruct")
    assert len(tf.Qwen2_5_VLForConditionalGeneration.calls) == 1 and tf.Qwen3VLForConditionalGeneration.calls == []
    assert loaded.image_config.image_patch_size == 14 and loaded.image_config.factor == 28


def test_missing_qwen3_vl_class_is_an_actionable_compatibility_error_not_a_fallback():
    tf = make_transformers(version="4.50.0", with_qwen3=False)
    tf.AutoModelForCausalLM = object()   # exists, but must NOT be substituted (TrapNamespace would fail on access)
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig(), torch_module=FakeTorch(GPU_T4), transformers_module=tf,
                        version_lookup=lookup_factory())
    err = exc.value
    assert err.code == "VLM_TRANSFORMERS_INCOMPATIBLE"
    assert "4.57.1" in err.message and "Qwen3-VL" in err.message or "qwen3_vl" in err.message
    assert "pip install" in err.message


def test_too_old_transformers_is_rejected_even_if_a_class_exists():
    tf = make_transformers(version="4.56.2")
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig(), torch_module=FakeTorch(GPU_T4), transformers_module=tf,
                        version_lookup=lookup_factory())
    assert exc.value.code == "VLM_TRANSFORMERS_INCOMPATIBLE" and "4.56.2" in exc.value.message
    assert tf.Qwen3VLForConditionalGeneration.calls == []


@pytest.mark.parametrize("checkpoint", [
    "meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen3-VL-30B-A3B-Instruct", "", "mock",
])
def test_unsupported_checkpoints_are_rejected_not_guessed(checkpoint):
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.resolve_family(checkpoint)
    assert exc.value.code == "VLM_UNSUPPORTED_MODEL"
    assert "Qwen3-VL" in exc.value.message and "text-only" in exc.value.message


def test_resolve_model_class_never_returns_a_generic_class():
    tf = make_transformers(with_qwen3=False)
    with pytest.raises(rt.VLMRuntimeError):
        rt.resolve_model_class(tf, rt.FAMILY_QWEN3_VL)


# ============================================================================
# 5-6. Dtype selection: BF16-capable and FP16-only hardware
# ============================================================================

@pytest.mark.parametrize("gpus,expected", [
    (GPU_A100, "bfloat16"), (GPU_L4, "bfloat16"),
    (GPU_T4, "float16"), (GPU_2xT4, "float16"), (GPU_V100, "float16"),
])
def test_auto_precision_uses_bf16_only_on_native_bf16_gpus(gpus, expected):
    # torch.cuda.is_bf16_supported() is faked to True everywhere: it must not drive the decision
    assert rt.select_dtype(rt.detect_hardware(FakeTorch(gpus)), "auto") == expected


def test_mixed_gpus_use_the_least_capable_device():
    hw = rt.detect_hardware(FakeTorch(GPU_A100 + GPU_T4))
    assert hw.native_bf16 is False and rt.select_dtype(hw, "auto") == "float16"


def test_hardware_detection_reports_name_capability_and_memory():
    info = rt.detect_hardware(FakeTorch(GPU_T4)).describe()
    assert info == [{"index": 0, "name": "Tesla T4", "compute_capability": "7.5", "total_memory_gib": 14.6}]


def test_explicit_bf16_on_a_t4_is_an_error_not_a_silent_downgrade():
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.select_dtype(rt.detect_hardware(FakeTorch(GPU_T4)), "bfloat16")
    assert exc.value.code == "VLM_DTYPE_UNSUPPORTED" and "float16" in exc.value.message and "7.5" in exc.value.message


def test_explicit_fp16_is_honoured_on_bf16_hardware():
    assert rt.select_dtype(rt.detect_hardware(FakeTorch(GPU_A100)), "float16") == "float16"


def test_no_gpu_is_an_explicit_failure_unless_cpu_is_opted_in():
    hw = rt.detect_hardware(FakeTorch([]))
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.select_dtype(hw, "auto")
    assert exc.value.code == "VLM_GPU_REQUIRED" and "NVIDIA GPU" in exc.value.message
    assert rt.select_dtype(hw, "auto", allow_cpu=True) == "float32"


def test_unknown_precision_value_is_rejected():
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.select_dtype(rt.detect_hardware(FakeTorch(GPU_T4)), "int4")
    assert exc.value.code == "VLM_DTYPE_UNSUPPORTED"


def test_loader_passes_the_selected_dtype_object_to_from_pretrained():
    t4, tf4 = load(GPU_T4)
    a100, tfa = load(GPU_A100)
    assert tf4.Qwen3VLForConditionalGeneration.calls[0]["dtype"] == "DTYPE_float16"
    assert tfa.Qwen3VLForConditionalGeneration.calls[0]["dtype"] == "DTYPE_bfloat16"
    assert t4.runtime_info["dtype"] == "float16" and a100.runtime_info["dtype"] == "bfloat16"
    assert "torch_dtype" not in tf4.Qwen3VLForConditionalGeneration.calls[0]     # deprecated spelling


# ============================================================================
# 7. 4-bit quantization configuration
# ============================================================================

def test_4bit_config_uses_nf4_double_quant_and_the_selected_compute_dtype():
    t4, tf = load(GPU_T4)
    quant = tf.Qwen3VLForConditionalGeneration.calls[0]["quantization_config"].kwargs
    assert quant == {
        "load_in_4bit": True, "bnb_4bit_compute_dtype": "DTYPE_float16",       # FP16 on a T4, not BF16
        "bnb_4bit_quant_type": "nf4", "bnb_4bit_use_double_quant": True,
    }
    assert t4.runtime_info["quantization"] == "bnb-nf4-4bit(compute=float16)"

    a100, tfa = load(GPU_A100)
    assert tfa.Qwen3VLForConditionalGeneration.calls[0]["quantization_config"].kwargs[
        "bnb_4bit_compute_dtype"] == "DTYPE_bfloat16"


def test_4bit_is_the_default_and_can_be_turned_off_explicitly():
    default, tf = load(GPU_A100)
    assert default.runtime_info["quantization"].startswith("bnb-nf4")
    full, tf2 = load(GPU_A100, load_in_4bit=False)
    call = tf2.Qwen3VLForConditionalGeneration.calls[0]
    assert "quantization_config" not in call and call["dtype"] == "DTYPE_bfloat16"
    assert full.runtime_info["quantization"] == "none"


@pytest.mark.parametrize("missing,fragment", [
    ({"bitsandbytes": None}, "bitsandbytes is not installed"),
    ({"bitsandbytes": "0.41.0"}, "older than the required 0.43.1"),
    ({"accelerate": None}, "accelerate is not installed"),
    ({"accelerate": "0.20.0"}, "older than the required 0.26.0"),
])
def test_4bit_with_missing_or_old_packages_fails_instead_of_loading_unquantized(missing, fragment):
    import importlib.metadata as md

    base = lookup_factory()

    def lookup(name):
        if name in missing:
            if missing[name] is None:
                raise md.PackageNotFoundError(name)
            return missing[name]
        return base(name)

    tf = make_transformers()
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig(), torch_module=FakeTorch(GPU_T4), transformers_module=tf, version_lookup=lookup)
    assert exc.value.code == "VLM_QUANTIZATION_UNAVAILABLE" and fragment in exc.value.message
    assert tf.Qwen3VLForConditionalGeneration.calls == []        # nothing was silently loaded in another precision


def test_4bit_without_cuda_or_with_float32_compute_is_rejected():
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.plan_quantization(rt.detect_hardware(FakeTorch([])), True, "float32", lookup_factory())
    assert exc.value.code == "VLM_QUANTIZATION_UNAVAILABLE"
    assert "CUDA" in exc.value.message and "bfloat16 or float16" in exc.value.message


def test_vram_preflight_blocks_unquantized_8b_on_a_t4_and_points_to_4bit():
    tf = make_transformers()
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig(load_in_4bit=False), torch_module=FakeTorch(GPU_T4),
                        transformers_module=tf, version_lookup=lookup_factory())
    assert exc.value.code == "VLM_INSUFFICIENT_VRAM"
    assert "load_in_4bit=True" in exc.value.message and "14.6" in exc.value.message
    assert tf.Qwen3VLForConditionalGeneration.calls == []


def test_vram_preflight_passes_4bit_on_a_t4_sums_multi_gpu_and_blocks_tiny_gpus():
    load(GPU_T4)                                              # 6 GiB needed < 14.6 available
    load(GPU_2xT4, load_in_4bit=False, precision="float16")   # 29.2 GiB across two T4s
    with pytest.raises(rt.VLMRuntimeError) as exc:
        load(GPU_SMALL)
    assert exc.value.code == "VLM_INSUFFICIENT_VRAM" and "Use a GPU with more memory" in exc.value.message
    load(GPU_SMALL, min_vram_gib=1.0)                         # explicit, documented override


# ============================================================================
# 8-9. Image processor: patch size, explicit pixel budget, no double resize
# ============================================================================

def test_image_patch_size_and_factor_per_family():
    assert rt.FAMILY_QWEN3_VL.image_patch_size == 16 and rt.FAMILY_QWEN3_VL.image_factor == 32
    assert rt.FAMILY_QWEN2_5_VL.image_patch_size == 14 and rt.FAMILY_QWEN2_5_VL.image_factor == 28


def test_default_pixel_budget_is_the_checkpoints_official_range():
    cfg = rt.build_image_config(rt.FAMILY_QWEN3_VL)
    assert (cfg.min_pixels, cfg.max_pixels) == (65536, 16777216)         # official shortest/longest_edge
    cfg25 = rt.build_image_config(rt.FAMILY_QWEN2_5_VL)
    assert (cfg25.min_pixels, cfg25.max_pixels) == (3136, 12845056)


def test_pixel_limits_are_validated_and_explicit_overrides_are_recorded():
    loaded, _ = load(GPU_T4, min_pixels=100000, max_pixels=2000000)
    assert loaded.runtime_info["image"] == {"image_patch_size": 16, "merge_size": 2,
                                           "min_pixels": 100000, "max_pixels": 2000000}
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.build_image_config(rt.FAMILY_QWEN3_VL, min_pixels=100, max_pixels=50)
    assert exc.value.code == "VLM_IMAGE_CONFIG_INVALID"
    with pytest.raises(rt.VLMRuntimeError):
        rt.build_image_config(rt.FAMILY_QWEN3_VL, min_pixels=10)


def test_processor_patch_size_mismatch_with_the_family_is_detected():
    tf = make_transformers(patch_size=14)             # a Qwen2.5-style processor for a Qwen3-VL checkpoint
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig(), torch_module=FakeTorch(GPU_T4), transformers_module=tf,
                        version_lookup=lookup_factory())
    assert exc.value.code == "VLM_PROCESSOR_MISMATCH"
    assert tf.Qwen3VLForConditionalGeneration.calls == []


def test_full_a4_page_at_200_dpi_is_not_downscaled_and_aspect_ratio_is_kept():
    cfg = rt.build_image_config(rt.FAMILY_QWEN3_VL)
    h, w = rt.expected_model_input_size(2339, 1654, cfg)          # A4 @ 200 DPI
    assert h % 32 == 0 and w % 32 == 0
    assert abs(h - 2339) <= 16 and abs(w - 1654) <= 16            # only rounding to the 32-px factor
    assert abs((h / w) - (2339 / 1654)) < 0.02
    assert 3500 < rt.estimate_visual_tokens(2339, 1654, cfg) < 4000


def test_only_pages_beyond_the_budget_are_scaled_and_never_distorted():
    cfg = rt.build_image_config(rt.FAMILY_QWEN3_VL)
    h, w = rt.expected_model_input_size(8000, 6000, cfg)          # 48 MP, far above 16.7 MP
    assert h * w <= cfg.max_pixels and h % 32 == 0 and w % 32 == 0
    assert abs((h / w) - (8000 / 6000)) < 0.02
    tiny = rt.expected_model_input_size(100, 100, cfg)            # below the minimum budget: scaled UP
    assert tiny[0] * tiny[1] >= cfg.min_pixels


class FakeProcessor:
    """Records exactly how the processor is called (and that no separate image-processing happens)."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.image_processor_called = False
        outer = self

        class _IP:
            def __call__(self, *a, **k):
                outer.image_processor_called = True
                raise AssertionError("the processor must not be pre-run through image_processor")

        self.image_processor = _IP()

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        self.last_messages = messages
        return "<chat>"

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return FakeInputs()

    def batch_decode(self, ids, **kwargs):
        return ['{"ok": true}']


class FakeInputs:
    input_ids = [[1, 2, 3]]

    def to(self, device):
        self.device = device
        return self

    def __iter__(self):
        return iter({"input_ids": self.input_ids}.items())

    def keys(self):
        return ["input_ids"]

    def __getitem__(self, key):
        return {"input_ids": self.input_ids}[key]


class FakeGenModel:
    device = "cuda:0"

    def __init__(self):
        self.generate_kwargs = None

    def generate(self, **kwargs):
        self.generate_kwargs = kwargs
        return [[1, 2, 3, 9, 9]]


def install_fake_utils(monkeypatch, with_patch_arg=True):
    calls: List[Dict[str, Any]] = []
    resized = object()

    if with_patch_arg:
        def process_vision_info(conversations, return_video_kwargs=False, return_video_metadata=False,
                                image_patch_size=14):
            calls.append({"messages": conversations, "image_patch_size": image_patch_size})
            return [resized], None
    else:
        def process_vision_info(conversations):          # an old qwen-vl-utils without image_patch_size
            calls.append({"messages": conversations})
            return [resized], None

    utils = types.ModuleType("qwen_vl_utils")
    utils.process_vision_info = process_vision_info
    monkeypatch.setitem(sys.modules, "qwen_vl_utils", utils)
    monkeypatch.setitem(sys.modules, "torch", FakeTorch(GPU_T4))
    return calls, resized


def extractor_with_fake_model(**kwargs):
    ext = SchemaGuidedExtractor(use_llm=True, allow_fallback=False, **kwargs)
    ext._model, ext._processor = FakeGenModel(), FakeProcessor()
    return ext


def test_generation_resizes_exactly_once_with_patch_16_and_explicit_pixel_limits(monkeypatch, tmp_path):
    calls, resized = install_fake_utils(monkeypatch)
    ext = extractor_with_fake_model()
    page = tmp_path / "p.png"
    Image.new("RGB", (100, 100), "white").save(page)

    text = ext._generate_from_image(page, "SYSTEM", "USER", 64)

    assert text == '{"ok": true}'
    # qwen-vl-utils: the single resize, with Qwen3-VL's 16-px patches (its default would be 14)
    assert len(calls) == 1 and calls[0]["image_patch_size"] == 16
    element = calls[0]["messages"][1]["content"][0]
    assert element["type"] == "image" and element["image"] == str(page)
    assert (element["min_pixels"], element["max_pixels"]) == (65536, 16777216)
    # processor: given the already-resized image, told not to resize again, never pre-processed separately
    (call,) = ext._processor.calls
    assert call["do_resize"] is False and call["images"] == [resized]
    assert ext._processor.image_processor_called is False
    # deterministic decoding and device placement from the model, not a hard-coded "cuda"
    assert ext._model.generate_kwargs["do_sample"] is False and ext._model.generate_kwargs["max_new_tokens"] == 64


def test_explicit_min_max_pixels_reach_the_image_element(monkeypatch, tmp_path):
    calls, _ = install_fake_utils(monkeypatch)
    ext = extractor_with_fake_model(min_pixels=200000, max_pixels=3000000)
    page = tmp_path / "p.png"
    Image.new("RGB", (50, 50)).save(page)
    ext._generate_from_image(page, "S", "U", 8)
    element = calls[0]["messages"][1]["content"][0]
    assert (element["min_pixels"], element["max_pixels"]) == (200000, 3000000)


def test_in_memory_bytes_are_decoded_to_an_image_object(monkeypatch):
    calls, _ = install_fake_utils(monkeypatch)
    ext = extractor_with_fake_model()
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buf, format="PNG")
    ext._generate_from_image(buf.getvalue(), "S", "U", 8)
    assert isinstance(calls[0]["messages"][1]["content"][0]["image"], Image.Image)


def test_old_qwen_vl_utils_without_patch_size_support_is_a_compatibility_error(monkeypatch, tmp_path):
    install_fake_utils(monkeypatch, with_patch_arg=False)
    ext = extractor_with_fake_model()
    page = tmp_path / "p.png"
    Image.new("RGB", (20, 20)).save(page)
    with pytest.raises(rt.VLMRuntimeError) as exc:
        ext._generate_from_image(page, "S", "U", 8)
    assert "qwen-vl-utils==0.0.14" in exc.value.message


def test_missing_qwen_vl_utils_names_the_pinned_baseline(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "qwen_vl_utils", None)         # import raises ImportError
    ext = extractor_with_fake_model()
    page = tmp_path / "p.png"
    Image.new("RGB", (20, 20)).save(page)
    with pytest.raises(rt.VLMRuntimeError) as exc:
        ext._generate_from_image(page, "S", "U", 8)
    assert exc.value.code == "VLM_DEPENDENCY_MISSING" and "qwen-vl-utils==0.0.14" in exc.value.message


# ============================================================================
# 10. Model-loading failures
# ============================================================================

def test_missing_pytorch_or_transformers_explains_the_separate_pytorch_install(monkeypatch):
    real_import = importlib.import_module

    def fake_import(name, *a, **k):
        if name in ("torch", "transformers"):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(rt.importlib, "import_module", fake_import)
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig())
    assert exc.value.code == "VLM_DEPENDENCY_MISSING"
    assert "PyTorch" in exc.value.message and "[vlm]" in exc.value.message


def test_out_of_memory_during_load_is_reported_as_insufficient_vram():
    tf = make_transformers()

    class OutOfMemoryError(RuntimeError):
        pass

    tf.Qwen3VLForConditionalGeneration.fail_with = OutOfMemoryError("CUDA out of memory. Tried to allocate 2 GiB")
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig(), torch_module=FakeTorch(GPU_T4), transformers_module=tf,
                        version_lookup=lookup_factory())
    assert exc.value.code == "VLM_INSUFFICIENT_VRAM" and "load_in_4bit" in exc.value.message


def test_any_other_load_failure_is_wrapped_with_the_cause():
    tf = make_transformers()
    tf.Qwen3VLForConditionalGeneration.fail_with = OSError("checkpoint files not found")
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig(), torch_module=FakeTorch(GPU_T4), transformers_module=tf,
                        version_lookup=lookup_factory())
    assert exc.value.code == "VLM_LOAD_FAILED" and "checkpoint files not found" in exc.value.message
    assert exc.value.details["cause_type"] == "OSError"


def test_processor_load_failure_is_wrapped():
    tf = make_transformers()
    tf.AutoProcessor = types.SimpleNamespace(from_pretrained=lambda *a, **k: (_ for _ in ()).throw(OSError("no net")))
    with pytest.raises(rt.VLMRuntimeError) as exc:
        rt.load_qwen_vl(rt.VLMLoadConfig(), torch_module=FakeTorch(GPU_T4), transformers_module=tf,
                        version_lookup=lookup_factory())
    assert exc.value.code == "VLM_LOAD_FAILED" and "processor" in exc.value.message


def test_extractor_does_not_retry_a_deterministic_load_failure_once_per_page(monkeypatch):
    attempts = []

    def failing_loader(config, **kwargs):
        attempts.append(config)
        raise rt.VLMRuntimeError("incompatible stack", code="VLM_TRANSFORMERS_INCOMPATIBLE")

    monkeypatch.setattr(rt, "load_qwen_vl", failing_loader)
    ext = SchemaGuidedExtractor(use_llm=True, allow_fallback=False)
    for _ in range(3):
        with pytest.raises(rt.VLMRuntimeError):
            ext._load_model()
    assert len(attempts) == 1


def test_out_of_memory_load_is_retryable_after_memory_is_freed(monkeypatch):
    attempts = []

    def loader(config, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise rt.VLMRuntimeError("oom", code="VLM_INSUFFICIENT_VRAM")
        return rt.LoadedVLM(model=FakeGenModel(), processor=FakeProcessor(), family=rt.FAMILY_QWEN3_VL,
                            image_config=rt.build_image_config(rt.FAMILY_QWEN3_VL), runtime_info={"dtype": "float16"})

    monkeypatch.setattr(rt, "load_qwen_vl", loader)
    ext = SchemaGuidedExtractor(use_llm=True, allow_fallback=False)
    with pytest.raises(rt.VLMRuntimeError):
        ext._load_model()
    model, _processor = ext._load_model()
    assert model is ext._model and ext.runtime_info == {"dtype": "float16"} and len(attempts) == 2


def test_load_failure_surfaces_through_the_pipeline_as_an_actionable_strict_failure(temp_storage, monkeypatch):
    run = _run(temp_storage, 2)
    monkeypatch.setattr(rt, "load_qwen_vl", lambda *a, **k: (_ for _ in ()).throw(rt.VLMRuntimeError(
        "transformers 4.50.0 is too old; need >= 4.57.1", code="VLM_TRANSFORMERS_INCOMPATIBLE")))
    pipeline = ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=True, allow_fallback=False))
    with pytest.raises(LLMExecutionError) as exc:
        pipeline.process(run.payload, page_images=run.refs)
    assert "need >= 4.57.1" in exc.value.message


def test_injected_model_and_processor_bypass_the_loader_entirely(monkeypatch):
    monkeypatch.setattr(rt, "load_qwen_vl", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not load")))
    ext = extractor_with_fake_model()
    model, processor = ext._load_model()
    assert model is ext._model and processor is ext._processor


# ============================================================================
# Recorded versions / runtime info
# ============================================================================

def test_exact_versions_and_loader_are_recorded_in_the_runtime_info():
    loaded, _ = load(GPU_T4, lookup=lookup_factory(transformers="4.57.1", qwen_vl_utils="0.0.14"))
    info = loaded.runtime_info
    assert info["versions"]["transformers"] == "4.57.1" and info["versions"]["qwen-vl-utils"] == "0.0.14"
    assert info["versions"]["accelerate"] == "1.10.0" and info["versions"]["bitsandbytes"] == "0.47.0"
    assert info["loader_class"] == "transformers.Qwen3VLForConditionalGeneration"
    assert info["qwen_vl_utils_matches_baseline"] is True
    assert info["devices"][0]["name"] == "Tesla T4"


def test_a_qwen_vl_utils_version_other_than_the_baseline_is_flagged_not_blocked(caplog):
    with caplog.at_level("WARNING", logger="doc_extractor"):
        loaded, _ = load(GPU_T4, lookup=lookup_factory(qwen_vl_utils="0.0.13"))
    assert loaded.runtime_info["qwen_vl_utils_matches_baseline"] is False
    assert "0.0.13" in caplog.text


def test_collect_runtime_versions_reports_missing_packages_as_none():
    import importlib.metadata as md

    def lookup(name):
        if name == "torch":
            raise md.PackageNotFoundError(name)
        return "1.2.3"

    versions = rt.collect_runtime_versions(lookup)
    assert versions["torch"] is None and versions["transformers"] == "1.2.3"


# ============================================================================
# 11. Bounding-box coordinate compatibility
# ============================================================================

def _page_ref(rotation=0, deskew=0.0, orig=(1654, 2339), prep=(1654, 2339)) -> ModuleAPageRef:
    return ModuleAPageRef(
        page_number=1, original_artifact_id="o", preprocessed_artifact_id="p",
        original_storage_uri="t/o.png", preprocessed_storage_uri="t/p.png",
        original_width_px=orig[0], original_height_px=orig[1],
        preprocessed_width_px=prep[0], preprocessed_height_px=prep[1],
        dpi=200, applied_rotation_deg=rotation, applied_deskew_deg=deskew, metadata_storage_uri="t/m.json",
    )


@pytest.mark.parametrize("rotation,deskew", [(0, 0.0), (90, 0.0), (0, 2.5), (180, -1.0)])
def test_normalized_boxes_map_through_module_a_geometry_independent_of_the_model_resize(rotation, deskew):
    ref = _page_ref(rotation=rotation, deskew=deskew)
    page = PageImageRef(page_number=1, image_path="p.png", page_ref=ref)
    bbox_norm = (100.0, 200.0, 150.0, 600.0)                        # [ymin, xmin, ymax, xmax] on 0-1000
    grounding = {"f": FieldGrounding(page_number=1, bbox_norm=bbox_norm, raw_value="v")}

    hint = grounding_to_evidence_hints(grounding, [page])["f"]

    ymin, xmin, ymax, xmax = bbox_norm
    w, h = ref.preprocessed_width_px, ref.preprocessed_height_px     # the PREPROCESSED page, not the model input
    expected = CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=(xmin * w / 1000.0, ymin * h / 1000.0, xmax * w / 1000.0, ymax * h / 1000.0),
        original_width=ref.original_width_px, original_height=ref.original_height_px,
        applied_rotation_deg=rotation, applied_deskew_deg=deskew,
    )
    assert hint["bbox"] == pytest.approx(expected)


def test_the_models_resized_input_size_does_not_change_the_mapping():
    """The model's relative [0,1000] grid is relative to ITS (resized) image: same fraction of the page."""
    cfg = rt.build_image_config(rt.FAMILY_QWEN3_VL)
    page_w, page_h = 1654, 2339
    model_h, model_w = rt.expected_model_input_size(page_h, page_w, cfg)
    assert (model_h, model_w) != (page_h, page_w)                    # the model really saw a different size
    # A real object at pixel box (x0,y0,x1,y1) on the page occupies the same FRACTIONS in the resized image,
    # so the normalized box a perfect model emits maps back to the same page pixels.
    x0, y0, x1, y1 = 330.8, 467.8, 992.4, 701.7
    emitted = (y0 / page_h * 1000, x0 / page_w * 1000, y1 / page_h * 1000, x1 / page_w * 1000)
    in_model_px = (emitted[1] * model_w / 1000, emitted[0] * model_h / 1000,
                   emitted[3] * model_w / 1000, emitted[2] * model_h / 1000)
    back = (in_model_px[0] / model_w * page_w, in_model_px[1] / model_h * page_h,
            in_model_px[2] / model_w * page_w, in_model_px[3] / model_h * page_h)
    assert back == pytest.approx((x0, y0, x1, y1))


def test_grounding_code_does_not_depend_on_any_image_resize_setting():
    import inspect

    import doc_extractor.vlm_grounding as grounding

    source = inspect.getsource(grounding)
    for forbidden in ("vlm_runtime", "min_pixels", "max_pixels", "image_patch_size", "smart_resize"):
        assert forbidden not in source


# ============================================================================
# 12. Existing mocked Qwen extraction keeps working (policy unchanged)
# ============================================================================

def test_mocked_multipage_extraction_is_unchanged_and_still_never_auto_approves(temp_storage, monkeypatch):
    run = _run(temp_storage, 2)
    PageAwareVLM({
        1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [100, 120, 150, 420]}},
        2: {"applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [200, 120, 250, 320]}},
    }).bind(run.refs).install(monkeypatch)

    env = _vlm_pipeline().process(run.payload, page_images=run.refs)

    assert env.fields["applicant.full_name"].evidence.page == 1
    assert env.fields["applicant.pan_card_number"].evidence.page == 2
    for field in env.fields.values():
        if field.value is not None:
            assert field.confidence is None and field.review_required is True
    assert env.document_metadata.status == "needs_review"
    assert "runtime" not in env.run.provenance.details          # nothing loaded in a mocked run


def test_runtime_info_of_a_loaded_model_is_recorded_in_the_run_provenance(temp_storage, monkeypatch):
    run = _run(temp_storage, 1)
    PageAwareVLM({1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [100, 120, 150, 420]}}}
                 ).bind(run.refs).install(monkeypatch)
    pipeline = _vlm_pipeline()
    pipeline.extractor.runtime_info = {"dtype": "float16", "loader_class": "transformers.Qwen3VLForConditionalGeneration",
                                       "versions": {"transformers": "4.57.1"}}

    env = pipeline.process(run.payload, page_images=run.refs)

    assert env.run.provenance.details["runtime"]["dtype"] == "float16"
    assert env.run.provenance.details["runtime"]["versions"]["transformers"] == "4.57.1"
    assert env.document_metadata.status == "needs_review"          # recording versions changes no policy


def test_extract_page_image_end_to_end_with_injected_fakes(monkeypatch, tmp_path):
    install_fake_utils(monkeypatch)
    ext = extractor_with_fake_model()
    ext._processor.batch_decode = lambda *a, **k: ['{"applicant.full_name": {"value": "A", "bbox": [1, 2, 3, 4]}}']
    page = tmp_path / "p.png"
    Image.new("RGB", (64, 64)).save(page)

    raw, prov = ext.extract_page_image(str(page), None, system_prompt="SYS")

    assert raw["applicant.full_name"]["value"] == "A"
    assert prov.provider == "transformers_qwen_vl" and prov.fallback_occurred is False


# ============================================================================
# Module B Qwen2.5-VL parser adapter: same loader, no hard-coded BF16, no double image processing
# ============================================================================

def _adapter_modules(monkeypatch, gpus):
    tf = make_transformers(version="4.57.1", patch_size=14)
    monkeypatch.setitem(sys.modules, "torch", FakeTorch(gpus))
    monkeypatch.setitem(sys.modules, "transformers", tf)
    monkeypatch.setattr(rt, "collect_runtime_versions", lambda *a, **k: {"transformers": "4.57.1"})
    monkeypatch.setattr(rt, "package_version", lambda name, lookup=None: {"bitsandbytes": "0.47.0",
                                                                         "accelerate": "1.10.0"}.get(name))
    monkeypatch.delitem(sys.modules, "doc_extractor.parsing.qwen_vl_adapter", raising=False)
    return tf


def test_parser_adapter_uses_the_shared_loader_with_fp16_on_a_t4(monkeypatch):
    tf = _adapter_modules(monkeypatch, GPU_T4)
    from doc_extractor.parsing.qwen_vl_adapter import QwenVLParserAdapter

    adapter = QwenVLParserAdapter()
    call = tf.Qwen2_5_VLForConditionalGeneration.calls[0]
    assert adapter.is_operational() and call["dtype"] == "DTYPE_float16"
    assert call["quantization_config"].kwargs["bnb_4bit_compute_dtype"] == "DTYPE_float16"
    monkeypatch.delitem(sys.modules, "doc_extractor.parsing.qwen_vl_adapter", raising=False)


def test_parser_adapter_fails_closed_with_the_actionable_reason(monkeypatch):
    _adapter_modules(monkeypatch, [])                 # no GPU
    from doc_extractor.parsing.qwen_vl_adapter import QwenVLParserAdapter

    adapter = QwenVLParserAdapter()
    assert adapter.is_operational() is False and "No CUDA GPU" in adapter._init_error
    monkeypatch.delitem(sys.modules, "doc_extractor.parsing.qwen_vl_adapter", raising=False)


def test_parser_adapter_runs_the_processor_once_on_the_pil_page(monkeypatch):
    import numpy as np

    tf = _adapter_modules(monkeypatch, GPU_A100)
    from doc_extractor.contracts_b import BoundingBox  # noqa: F401
    from doc_extractor.loader_b import LoadedPageImage
    from doc_extractor.parsing.qwen_vl_adapter import QwenVLParserAdapter

    adapter = QwenVLParserAdapter()
    fake_processor = FakeProcessor()
    fake_processor.batch_decode = lambda *a, **k: ["[100, 100, 150, 400] BANK NAME: ACME"]
    adapter.processor, adapter.model = fake_processor, FakeGenModel()
    ref = _page_ref(orig=(1000, 1400), prep=(1000, 1400))
    page = LoadedPageImage(page_number=1, image_bgr=np.full((1400, 1000, 3), 255, dtype=np.uint8),
                           width_px=1000, height_px=1400, dpi=200, file_path="p.png", page_ref=ref)

    layout = adapter.parse_page(page)

    (call,) = fake_processor.calls
    assert len(call["images"]) == 1 and isinstance(call["images"][0], Image.Image)
    assert fake_processor.image_processor_called is False
    assert adapter.model.generate_kwargs["do_sample"] is False
    assert layout.blocks and layout.blocks[0].ocr_confidence is None
    monkeypatch.delitem(sys.modules, "doc_extractor.parsing.qwen_vl_adapter", raising=False)
