"""
Qwen-VL runtime readiness: model loading, hardware-aware precision and image-processor settings
(PRD v1.3 ARC-002 / NFR-007 / NFR-008, GR-016).

Everything here is import-safe without PyTorch or Transformers: ``torch`` / ``transformers`` are
imported lazily (or injected, which is how the unit tests run on a machine without a GPU). Nothing
here produces or alters confidence values or review gating.

Verified facts this module encodes (checked against the published package sources, not memory):

* Qwen3-VL needs ``transformers >= 4.57.1`` (``Qwen3VLForConditionalGeneration`` first ships in
  4.57.0, which was yanked; 4.56.x has no Qwen3-VL).
* Qwen3-VL image processing: ``patch_size=16`` and ``merge_size=2`` (so images must be a multiple
  of 32 px); Qwen2.5-VL uses ``patch_size=14`` (multiple of 28 px). ``qwen_vl_utils.process_vision_info``
  defaults to 14, so Qwen3-VL callers MUST pass ``image_patch_size=16``.
* The checkpoint's official pixel limits are ``size.shortest_edge=65536`` / ``size.longest_edge=16777216``
  (Qwen3-VL) and ``min_pixels=3136`` / ``max_pixels=12845056`` (Qwen2.5-VL). Those are the defaults
  here, so a full 200-DPI A4 page (~3.9 MP) is never silently down-scaled.
* ``qwen-vl-utils`` already resizes the image (aspect ratio kept, rounded to the patch factor), so the
  processor is called with ``do_resize=False`` to avoid resizing twice.
* The Qwen3-VL model emits boxes as ``[ymin, xmin, ymax, xmax]`` on a 0-1000 grid *relative to the
  image it saw*; that is independent of any resize, which is why the grounding code can map them with
  the Module A page geometry (see ``vlm_grounding``).
* Native BF16 needs an NVIDIA Ampere-or-newer GPU (compute capability >= 8.0). A Tesla T4 (7.5) must
  use FP16. ``torch.cuda.is_bf16_supported()`` is NOT used: recent PyTorch reports True on older GPUs
  by emulation, which is slow and not what we want to rely on.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from doc_extractor.exceptions import DocumentExtractorError

logger = logging.getLogger("doc_extractor")

# --------------------------------------------------------------------------------------------
# Constants (single source of truth; README and requirements mirror them)
# --------------------------------------------------------------------------------------------

QWEN3_VL_4B_MODEL_ID = "Qwen/Qwen3-VL-4B-Instruct"
QWEN3_VL_8B_MODEL_ID = "Qwen/Qwen3-VL-8B-Instruct"
QWEN3_VL_MODEL_ID = QWEN3_VL_4B_MODEL_ID     # primary documented checkpoint (fits a 16 GB T4 in 4-bit)
QWEN2_5_VL_MODEL_ID = "Qwen/Qwen2.5-VL-7B-Instruct"


def _env_int(name: str, default: int) -> int:
    """Positive integer from the environment; an unset, malformed or non-positive value uses ``default``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring %s=%r (not an integer); using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Ignoring %s=%d (must be positive); using %d", name, value, default)
        return default
    return value


# Default checkpoint for routing and the extractor: DOC_DEFAULT_MODEL, else Qwen3-VL-4B-Instruct. Set
# DOC_DEFAULT_MODEL=Qwen/Qwen3-VL-8B-Instruct on a bigger (>= 24 GB) GPU for higher accuracy.
DEFAULT_VLM_MODEL_ID = os.environ.get("DOC_DEFAULT_MODEL") or QWEN3_VL_4B_MODEL_ID

# Cap on the pixels of the page image sent to the vision model. The vision encoder's attention memory grows
# with the SQUARE of the pixel count, and 2.0 MP already ran a 16 GB T4 out of memory, so the default is
# 1.3 MP (about 120 DPI for A4). DOC_MAX_IMAGE_PIXELS=4200000 keeps full 200 DPI on a bigger GPU. The cap
# is applied explicitly and recorded in the run info; it is never lowered silently after an OOM.
DEFAULT_MAX_IMAGE_PIXELS = _env_int("DOC_MAX_IMAGE_PIXELS", 1_300_000)
# Classification (category, language, rotation) does not need 200 DPI: a small image is far faster.
DEFAULT_CLASSIFY_MAX_PIXELS = _env_int("DOC_CLASSIFY_MAX_PIXELS", 500_000)

MIN_TRANSFORMERS_QWEN3_VL = "4.57.1"
MIN_TRANSFORMERS_QWEN2_5_VL = "4.49.0"
QWEN_VL_UTILS_BASELINE = "0.0.14"           # exact version of the reproducible GPU baseline
MIN_ACCELERATE = "0.26.0"                    # transformers' own ACCELERATE_MIN_VERSION (4.57.1)
MIN_BITSANDBYTES = "0.43.1"                  # transformers' own floor for 4-bit (4.57.1)

# Pre-flight minimum VRAM (GiB) to ATTEMPT a load. These are the PRD NFR-007 figures for an 8B model
# (~16 GB unquantized, ~6 GB with 4-bit) and are used when the checkpoint's size cannot be read from
# its name. For a checkpoint whose name states its size (``...-4B-Instruct``) the figure is derived
# from the parameter count by ``estimate_min_vram_gib``. Excludes activations / KV cache, so passing it
# does not guarantee a page fits.
MIN_VRAM_GIB_UNQUANTIZED = 16.0
MIN_VRAM_GIB_4BIT = 6.0
BYTES_PER_PARAM_16BIT = 2.0                  # bf16 / fp16 weights: 8B -> 16 GiB
BYTES_PER_PARAM_NF4 = 0.5                    # 4-bit weights ...
NF4_FIXED_OVERHEAD_GIB = 2.0                 # ... plus vision tower / embeddings / CUDA context: 8B -> 6 GiB

NATIVE_BF16_MIN_CAPABILITY = (8, 0)         # NVIDIA Ampere (A100, A10, L4, RTX 30xx/40xx)

PRECISIONS = ("auto", "bfloat16", "float16", "float32")

# trust_remote_code executes Python shipped inside a model repository, so it is ALLOWLISTED per model
# (exact repo id). Qwen3-VL / Qwen2.5-VL and Qwen3-4B load through native Transformers classes and are
# never allowed to run remote code. Documented exceptions (id -> why):
TRUST_REMOTE_CODE_ALLOWLIST: Dict[str, str] = {
    "zai-org/GLM-OCR": (
        "v1.5 OCRTextAdapter loads GLM-OCR with its repository code (model-card usage; the 'glm_ocr' "
        "architecture may not exist in the installed Transformers). It is UNVERIFIED on real hardware: "
        "remove this entry once the installed Transformers is confirmed to load it natively."
    ),
}
TRUST_REMOTE_CODE_ENV = "DOC_TRUST_REMOTE_CODE_MODELS"   # comma-separated extra model ids (explicit opt-in)


class VLMRuntimeError(DocumentExtractorError):
    """
    A Qwen-VL runtime/compatibility problem with an actionable message. Never exposes document
    content. ``code`` is a stable machine-readable identifier:

    VLM_DEPENDENCY_MISSING, VLM_TRANSFORMERS_INCOMPATIBLE, VLM_UNSUPPORTED_MODEL, VLM_GPU_REQUIRED,
    VLM_DTYPE_UNSUPPORTED, VLM_QUANTIZATION_UNAVAILABLE, VLM_INSUFFICIENT_VRAM,
    VLM_PROCESSOR_MISMATCH, VLM_IMAGE_CONFIG_INVALID, VLM_LOAD_FAILED, VLM_REMOTE_CODE_NOT_ALLOWED.
    """

    def __init__(self, message: str, code: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message=message, code=code, details=details)


# --------------------------------------------------------------------------------------------
# Model family
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VLMFamily:
    name: str
    model_class_name: str          # the explicit Transformers class; no generic fallbacks
    image_patch_size: int
    merge_size: int
    min_transformers: str
    default_min_pixels: int
    default_max_pixels: int

    @property
    def image_factor(self) -> int:
        """Images must be a multiple of this many pixels per side."""
        return self.image_patch_size * self.merge_size


FAMILY_QWEN3_VL = VLMFamily(
    name="qwen3_vl",
    model_class_name="Qwen3VLForConditionalGeneration",
    image_patch_size=16,
    merge_size=2,
    min_transformers=MIN_TRANSFORMERS_QWEN3_VL,
    default_min_pixels=65536,
    default_max_pixels=16777216,
)
FAMILY_QWEN2_5_VL = VLMFamily(
    name="qwen2_5_vl",
    model_class_name="Qwen2_5_VLForConditionalGeneration",
    image_patch_size=14,
    merge_size=2,
    min_transformers=MIN_TRANSFORMERS_QWEN2_5_VL,
    default_min_pixels=3136,
    default_max_pixels=12845056,
)


def resolve_family(model_id: str) -> VLMFamily:
    """Maps a checkpoint id/path to its model family; unknown checkpoints are rejected, not guessed."""
    name = (model_id or "").lower().replace("_", "-")
    if "qwen3-vl" in name and "moe" not in name and "a3b" not in name and "a22b" not in name:
        return FAMILY_QWEN3_VL
    if "qwen2.5-vl" in name:
        return FAMILY_QWEN2_5_VL
    raise VLMRuntimeError(
        f"Unsupported VLM checkpoint '{model_id}'. Supported: Qwen3-VL dense checkpoints such as "
        f"'{QWEN3_VL_MODEL_ID}' (primary) and Qwen2.5-VL such as '{QWEN2_5_VL_MODEL_ID}'. "
        "No generic text-only loader is substituted.",
        code="VLM_UNSUPPORTED_MODEL",
        details={"model_id": model_id},
    )


def _parse_version(text: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for piece in str(text).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_at_least(installed: str, minimum: str) -> bool:
    return _parse_version(installed) >= _parse_version(minimum)


def package_version(name: str, lookup: Callable[[str], str] = importlib.metadata.version) -> Optional[str]:
    try:
        return lookup(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def resolve_model_class(transformers_module: Any, family: VLMFamily) -> Any:
    """
    Returns the exact Transformers class for ``family`` or raises an actionable error. Never
    falls back to ``AutoModelForCausalLM`` / ``AutoModel`` (text-only loaders that would load the
    wrong architecture or fail obscurely).
    """
    cls = getattr(transformers_module, family.model_class_name, None)
    if cls is None:
        installed = getattr(transformers_module, "__version__", "unknown")
        raise VLMRuntimeError(
            f"The installed transformers ({installed}) does not provide {family.model_class_name}, which "
            f"{family.name} requires. Install transformers>={family.min_transformers}: "
            f"pip install \"transformers>={family.min_transformers}\" (it is a core dependency: pip install -e .).",
            code="VLM_TRANSFORMERS_INCOMPATIBLE",
            details={"required_class": family.model_class_name, "installed_transformers": installed,
                     "minimum_transformers": family.min_transformers},
        )
    return cls


# --------------------------------------------------------------------------------------------
# trust_remote_code policy and model sizing
# --------------------------------------------------------------------------------------------

def trust_remote_code_allowed(model_id: str) -> bool:
    """True only for allowlisted repos (``TRUST_REMOTE_CODE_ALLOWLIST`` or the explicit env opt-in)."""
    name = (model_id or "").strip().lower()
    extra = {m.strip().lower() for m in os.environ.get(TRUST_REMOTE_CODE_ENV, "").split(",") if m.strip()}
    return name in {k.lower() for k in TRUST_REMOTE_CODE_ALLOWLIST} | extra


def resolve_trust_remote_code(model_id: str, requested: bool = False) -> bool:
    """
    The ``trust_remote_code`` value to use for ``model_id``: True only for an allowlisted repository,
    False for everything else. Explicitly requesting it for a non-allowlisted model is refused rather
    than honoured or silently ignored.
    """
    allowed = trust_remote_code_allowed(model_id)
    if requested and not allowed:
        raise VLMRuntimeError(
            f"trust_remote_code=True is not allowed for '{model_id}': it would execute code from the model "
            "repository. Only allowlisted models may (see vlm_runtime.TRUST_REMOTE_CODE_ALLOWLIST or the "
            f"{TRUST_REMOTE_CODE_ENV} environment variable); Qwen-VL loads through native Transformers classes.",
            code="VLM_REMOTE_CODE_NOT_ALLOWED", details={"model_id": model_id},
        )
    return allowed


def model_params_billions(model_id: str) -> Optional[float]:
    """Parameter count (billions) stated in a checkpoint name such as ``Qwen3-VL-4B-Instruct``; else None."""
    tail = (model_id or "").lower().replace("_", "-").rsplit("/", 1)[-1]
    match = re.search(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z])", tail)
    return float(match.group(1)) if match else None


def estimate_min_vram_gib(model_id: str, quantized: bool) -> float:
    """
    Model-aware pre-flight minimum (GiB) to attempt a load: weights at 2 bytes/parameter unquantized, or
    0.5 bytes/parameter + a fixed overhead for 4-bit. Reproduces the PRD figures for 8B (16 / 6 GiB) and
    scales them to the checkpoint (4B: 8 / 4 GiB). An unknown size keeps the conservative 8B figures.
    """
    billions = model_params_billions(model_id)
    if billions is None:
        return MIN_VRAM_GIB_4BIT if quantized else MIN_VRAM_GIB_UNQUANTIZED
    if quantized:
        return round(billions * BYTES_PER_PARAM_NF4 + NF4_FIXED_OVERHEAD_GIB, 2)
    return round(billions * BYTES_PER_PARAM_16BIT, 2)


def resolve_device_map(device_map: str, hardware: "HardwareInfo") -> str:
    """``single`` (the default; DOC_DEVICE_MAP) puts the whole model on GPU 0, which is faster than
    sharding layers across cards; ``auto`` / ``balanced`` / ``cuda:N`` are passed through unchanged."""
    if device_map == "single":
        return "cuda:0" if hardware.cuda_available else "auto"
    return device_map


# --------------------------------------------------------------------------------------------
# Hardware and precision
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class GPUInfo:
    index: int
    name: str
    capability: Tuple[int, int]
    total_memory_gib: float


@dataclass(frozen=True)
class HardwareInfo:
    gpus: Tuple[GPUInfo, ...] = ()

    @property
    def cuda_available(self) -> bool:
        return bool(self.gpus)

    @property
    def min_capability(self) -> Optional[Tuple[int, int]]:
        return min((g.capability for g in self.gpus), default=None)

    @property
    def native_bf16(self) -> bool:
        cap = self.min_capability
        return cap is not None and cap >= NATIVE_BF16_MIN_CAPABILITY

    @property
    def total_memory_gib(self) -> float:
        return sum(g.total_memory_gib for g in self.gpus)

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {"index": g.index, "name": g.name, "compute_capability": f"{g.capability[0]}.{g.capability[1]}",
             "total_memory_gib": round(g.total_memory_gib, 2)}
            for g in self.gpus
        ]


def detect_hardware(torch_module: Any) -> HardwareInfo:
    """Reads CUDA devices from ``torch`` (injectable for tests); no CUDA -> an empty HardwareInfo."""
    cuda = getattr(torch_module, "cuda", None)
    if cuda is None or not cuda.is_available():
        return HardwareInfo()
    gpus = []
    for index in range(int(cuda.device_count())):
        props = cuda.get_device_properties(index)
        gpus.append(GPUInfo(
            index=index,
            name=str(props.name),
            capability=(int(props.major), int(props.minor)),
            total_memory_gib=float(props.total_memory) / float(1024 ** 3),
        ))
    return HardwareInfo(gpus=tuple(gpus))


def select_dtype(hardware: HardwareInfo, requested: str = "auto", allow_cpu: bool = False) -> str:
    """
    Chooses the compute dtype name ("bfloat16" | "float16" | "float32").

    * ``auto``: BF16 only on GPUs with native support (compute capability >= 8.0), otherwise FP16
      (e.g. Tesla T4). Never silently picks something the user did not ask for or the GPU lacks.
    * explicit ``bfloat16`` on a GPU without native BF16 is an ERROR (no silent change of precision).
    * no GPU: an error unless ``allow_cpu`` (then float32 for ``auto``).
    """
    if requested not in PRECISIONS:
        raise VLMRuntimeError(
            f"Unknown precision '{requested}'. Use one of {', '.join(PRECISIONS)}.",
            code="VLM_DTYPE_UNSUPPORTED", details={"requested": requested},
        )
    if not hardware.cuda_available:
        if not allow_cpu:
            raise VLMRuntimeError(
                "No CUDA GPU detected. Qwen-VL extraction needs an NVIDIA GPU (4-bit quantization also requires "
                "CUDA). Run on a GPU host (e.g. Kaggle T4), or pass allow_cpu=True with load_in_4bit=False to "
                "accept a very slow, memory-hungry CPU run.",
                code="VLM_GPU_REQUIRED",
            )
        return "float32" if requested == "auto" else requested

    if requested == "auto":
        return "bfloat16" if hardware.native_bf16 else "float16"
    if requested == "bfloat16" and not hardware.native_bf16:
        cap = hardware.min_capability
        raise VLMRuntimeError(
            f"BF16 was requested but the GPU (compute capability {cap[0]}.{cap[1]}) has no native BF16 "
            f"(needs >= {NATIVE_BF16_MIN_CAPABILITY[0]}.{NATIVE_BF16_MIN_CAPABILITY[1]}, i.e. Ampere or newer). "
            "Use precision='float16' (or 'auto') on this GPU, e.g. a Tesla T4.",
            code="VLM_DTYPE_UNSUPPORTED",
            details={"requested": requested, "compute_capability": f"{cap[0]}.{cap[1]}"},
        )
    return requested


@dataclass(frozen=True)
class QuantizationPlan:
    enabled: bool
    quant_type: str = "nf4"
    double_quant: bool = True
    compute_dtype: Optional[str] = None

    def describe(self) -> str:
        return f"bnb-{self.quant_type}-4bit(compute={self.compute_dtype})" if self.enabled else "none"


def plan_quantization(
    hardware: HardwareInfo,
    load_in_4bit: bool,
    dtype: str,
    version_lookup: Callable[[str], str] = importlib.metadata.version,
) -> QuantizationPlan:
    """
    Validates that 4-bit loading can work and ties its compute dtype to the selected dtype.
    4-bit (bitsandbytes) needs CUDA, bitsandbytes >= 0.43.1, accelerate >= 0.26.0 and a 16-bit
    compute dtype. Nothing is silently downgraded to non-quantized loading.
    """
    if not load_in_4bit:
        return QuantizationPlan(enabled=False)
    problems: List[str] = []
    if not hardware.cuda_available:
        problems.append("4-bit quantization (bitsandbytes) requires a CUDA GPU")
    if dtype not in ("bfloat16", "float16"):
        problems.append(f"4-bit compute dtype must be bfloat16 or float16, got {dtype}")
    bnb = package_version("bitsandbytes", version_lookup)
    if bnb is None:
        problems.append(f"bitsandbytes is not installed (pip install \"bitsandbytes>={MIN_BITSANDBYTES}\")")
    elif not version_at_least(bnb, MIN_BITSANDBYTES):
        problems.append(f"bitsandbytes {bnb} is older than the required {MIN_BITSANDBYTES}")
    acc = package_version("accelerate", version_lookup)
    if acc is None:
        problems.append(f"accelerate is not installed (pip install \"accelerate>={MIN_ACCELERATE}\")")
    elif not version_at_least(acc, MIN_ACCELERATE):
        problems.append(f"accelerate {acc} is older than the required {MIN_ACCELERATE}")
    if problems:
        raise VLMRuntimeError(
            "4-bit quantization is unavailable: " + "; ".join(problems) + ". Fix the above, or set "
            "load_in_4bit=False on a GPU with enough memory (>= ~16 GiB for the 8B model).",
            code="VLM_QUANTIZATION_UNAVAILABLE",
            details={"problems": problems, "bitsandbytes": bnb, "accelerate": acc},
        )
    return QuantizationPlan(enabled=True, compute_dtype=dtype)


def build_bnb_config(transformers_module: Any, plan: QuantizationPlan, dtype_object: Any) -> Any:
    """BitsAndBytesConfig whose compute dtype is the SELECTED dtype (never a hard-coded BF16)."""
    config_cls = getattr(transformers_module, "BitsAndBytesConfig", None)
    if config_cls is None:
        raise VLMRuntimeError(
            "The installed transformers does not provide BitsAndBytesConfig; upgrade transformers "
            f"(>= {MIN_TRANSFORMERS_QWEN3_VL}).",
            code="VLM_TRANSFORMERS_INCOMPATIBLE",
        )
    return config_cls(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=dtype_object,
        bnb_4bit_quant_type=plan.quant_type,
        bnb_4bit_use_double_quant=plan.double_quant,
    )


def check_vram(
    hardware: HardwareInfo,
    quantized: bool,
    min_vram_gib: Optional[float] = None,
    model_id: Optional[str] = None,
    device_map: str = "auto",
) -> None:
    """
    Pre-flight VRAM check with an actionable error. The requirement is model-aware
    (``estimate_min_vram_gib``) unless ``min_vram_gib`` is given. With a single-device map
    (``single`` / ``cuda:N``) only that GPU's memory counts; otherwise the memory of all GPUs is summed.
    """
    if not hardware.cuda_available:
        return
    needed = min_vram_gib if min_vram_gib is not None else estimate_min_vram_gib(model_id or "", quantized)
    gpus = hardware.gpus
    if device_map == "single":
        gpus = gpus[:1]
    elif device_map.startswith("cuda:") and device_map[5:].isdigit():
        gpus = tuple(g for g in gpus if g.index == int(device_map[5:])) or gpus[:1]
    available = sum(g.total_memory_gib for g in gpus)
    if available + 1e-6 < needed:
        hint = (
            "Use a GPU with more memory." if quantized else
            "Enable 4-bit quantization (load_in_4bit=True), which needs roughly "
            f"{estimate_min_vram_gib(model_id or '', True):g} GiB, or use a larger GPU."
        )
        raise VLMRuntimeError(
            f"Insufficient GPU memory to load the model: {available:.1f} GiB available across "
            f"{len(gpus)} GPU(s), at least {needed:g} GiB required "
            f"({'4-bit' if quantized else 'unquantized'}). {hint}",
            code="VLM_INSUFFICIENT_VRAM",
            details={"available_gib": round(available, 2), "required_gib": needed, "quantized": quantized,
                     "model_id": model_id},
        )


# --------------------------------------------------------------------------------------------
# Image processing configuration
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ImageConfig:
    """
    Explicit image settings for ``qwen_vl_utils.process_vision_info`` (the ONLY resize step; the
    processor is then called with ``do_resize=False``). Aspect ratio is preserved (each side is
    rounded to a multiple of ``factor``); the pixel budget is the checkpoint's official range unless
    overridden. Lowering ``max_pixels`` trades accuracy for memory and is recorded in the run info.
    """
    image_patch_size: int
    merge_size: int
    min_pixels: int
    max_pixels: int

    @property
    def factor(self) -> int:
        return self.image_patch_size * self.merge_size

    def describe(self) -> Dict[str, int]:
        return {"image_patch_size": self.image_patch_size, "merge_size": self.merge_size,
                "min_pixels": self.min_pixels, "max_pixels": self.max_pixels}


def build_image_config(
    family: VLMFamily,
    min_pixels: Optional[int] = None,
    max_pixels: Optional[int] = None,
) -> ImageConfig:
    lo = family.default_min_pixels if min_pixels is None else int(min_pixels)
    hi = family.default_max_pixels if max_pixels is None else int(max_pixels)
    factor = family.image_factor
    if lo < factor * factor:
        raise VLMRuntimeError(
            f"min_pixels={lo} is smaller than one {factor}x{factor} patch block ({factor * factor}).",
            code="VLM_IMAGE_CONFIG_INVALID", details={"min_pixels": lo, "max_pixels": hi},
        )
    if hi < lo:
        raise VLMRuntimeError(
            f"max_pixels={hi} must be >= min_pixels={lo}.",
            code="VLM_IMAGE_CONFIG_INVALID", details={"min_pixels": lo, "max_pixels": hi},
        )
    return ImageConfig(family.image_patch_size, family.merge_size, lo, hi)


def image_message_element(image: Any, config: ImageConfig) -> Dict[str, Any]:
    """The chat-message image element: carries the explicit pixel limits to qwen-vl-utils."""
    return {"type": "image", "image": image, "min_pixels": config.min_pixels, "max_pixels": config.max_pixels}


def expected_model_input_size(height: int, width: int, config: ImageConfig) -> Tuple[int, int]:
    """
    (height, width) the model will see after the single qwen-vl-utils resize. Mirrors
    ``qwen_vl_utils.smart_resize`` (0.0.14): sides rounded to the patch factor, aspect ratio kept,
    pixel count clamped to [min_pixels, max_pixels]. Used for token budgeting and tests; the library
    remains the source of truth at runtime.
    """
    factor = config.factor
    h_bar = max(factor, round(height / factor) * factor)
    w_bar = max(factor, round(width / factor) * factor)
    if h_bar * w_bar > config.max_pixels:
        beta = math.sqrt((height * width) / config.max_pixels)
        h_bar = math.floor(height / beta / factor) * factor
        w_bar = math.floor(width / beta / factor) * factor
    elif h_bar * w_bar < config.min_pixels:
        beta = math.sqrt(config.min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def estimate_visual_tokens(height: int, width: int, config: ImageConfig) -> int:
    h, w = expected_model_input_size(height, width, config)
    return (h // config.factor) * (w // config.factor)


def vision_info_function(qwen_vl_utils_module: Any, family: VLMFamily) -> Callable[..., Any]:
    """
    ``process_vision_info`` from qwen-vl-utils, verified to accept ``image_patch_size`` (needed to
    resize correctly for Qwen3-VL's 16 px patches; older releases hard-code 14).
    """
    import inspect

    fn = getattr(qwen_vl_utils_module, "process_vision_info", None)
    if fn is None:
        raise VLMRuntimeError(
            "qwen_vl_utils.process_vision_info not found; install the pinned baseline: "
            f"pip install qwen-vl-utils=={QWEN_VL_UTILS_BASELINE}.",
            code="VLM_DEPENDENCY_MISSING",
        )
    if "image_patch_size" not in inspect.signature(fn).parameters and family.image_patch_size != 14:
        raise VLMRuntimeError(
            f"The installed qwen-vl-utils cannot resize for {family.image_patch_size}-px patches (no "
            f"image_patch_size argument). Install qwen-vl-utils=={QWEN_VL_UTILS_BASELINE}.",
            code="VLM_TRANSFORMERS_INCOMPATIBLE",
        )
    return fn


# --------------------------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------------------------

@dataclass
class VLMLoadConfig:
    model_id: str = DEFAULT_VLM_MODEL_ID
    load_in_4bit: bool = True
    precision: str = "auto"
    allow_cpu: bool = False
    device_map: str = field(default_factory=lambda: os.environ.get("DOC_DEVICE_MAP", "single"))
    trust_remote_code: bool = False
    attn_implementation: Optional[str] = "sdpa"   # memory-efficient attention; eager materialises huge matrices
    min_pixels: Optional[int] = None
    max_pixels: Optional[int] = None
    min_vram_gib: Optional[float] = None


@dataclass
class LoadedVLM:
    model: Any
    processor: Any
    family: VLMFamily
    image_config: ImageConfig
    runtime_info: Dict[str, Any] = field(default_factory=dict)


def collect_runtime_versions(lookup: Callable[[str], str] = importlib.metadata.version) -> Dict[str, Optional[str]]:
    """Exact installed versions relevant to reproducing a run (None when not installed)."""
    return {
        "python": ".".join(str(p) for p in sys.version_info[:3]),
        **{name: package_version(name, lookup) for name in
           ("transformers", "torch", "torchvision", "qwen-vl-utils", "accelerate", "bitsandbytes")},
    }


def _is_oom(exc: BaseException) -> bool:
    return type(exc).__name__ in ("OutOfMemoryError", "CudaOutOfMemoryError") or "out of memory" in str(exc).lower()


def load_qwen_vl(
    config: VLMLoadConfig,
    torch_module: Any = None,
    transformers_module: Any = None,
    version_lookup: Callable[[str], str] = importlib.metadata.version,
) -> LoadedVLM:
    """
    Loads the processor and model with explicit, verified settings. Raises ``VLMRuntimeError`` with
    an actionable message for every incompatibility; there is no silent fallback to another loader,
    dtype or quantization. ``torch_module`` / ``transformers_module`` are injectable for tests.
    """
    family = resolve_family(config.model_id)

    try:
        torch_module = torch_module or importlib.import_module("torch")
        transformers_module = transformers_module or importlib.import_module("transformers")
    except ImportError as exc:
        raise VLMRuntimeError(
            f"Qwen-VL dependencies are not installed ({exc}). Install PyTorch for your platform FIRST "
            "(see README: https://pytorch.org/get-started/locally/), then: pip install -e . "
            "(transformers, accelerate, bitsandbytes and qwen-vl-utils are core dependencies).",
            code="VLM_DEPENDENCY_MISSING",
        ) from exc

    installed_tf = getattr(transformers_module, "__version__", None) or package_version("transformers", version_lookup)
    if installed_tf is not None and not version_at_least(installed_tf, family.min_transformers):
        raise VLMRuntimeError(
            f"transformers {installed_tf} is too old for {family.name}; need >= {family.min_transformers}: "
            f"pip install \"transformers>={family.min_transformers}\".",
            code="VLM_TRANSFORMERS_INCOMPATIBLE",
            details={"installed_transformers": installed_tf, "minimum_transformers": family.min_transformers},
        )
    model_cls = resolve_model_class(transformers_module, family)

    hardware = detect_hardware(torch_module)
    dtype_name = select_dtype(hardware, config.precision, allow_cpu=config.allow_cpu)
    plan = plan_quantization(hardware, config.load_in_4bit, dtype_name, version_lookup)
    device_map = resolve_device_map(config.device_map, hardware)
    check_vram(hardware, plan.enabled, config.min_vram_gib, model_id=config.model_id, device_map=device_map)
    trust_remote_code = resolve_trust_remote_code(config.model_id, config.trust_remote_code)
    image_config = build_image_config(family, config.min_pixels, config.max_pixels)
    dtype_object = getattr(torch_module, dtype_name)

    try:
        processor = transformers_module.AutoProcessor.from_pretrained(
            config.model_id, trust_remote_code=trust_remote_code
        )
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        raise VLMRuntimeError(
            f"Could not load the processor for '{config.model_id}': {exc}",
            code="VLM_LOAD_FAILED", details={"cause_type": type(exc).__name__},
        ) from exc
    patch = getattr(getattr(processor, "image_processor", None), "patch_size", None)
    if patch is not None and int(patch) != family.image_patch_size:
        raise VLMRuntimeError(
            f"Processor patch_size={patch} does not match the expected {family.image_patch_size} for "
            f"{family.name}; the checkpoint and model family disagree.",
            code="VLM_PROCESSOR_MISMATCH", details={"processor_patch_size": int(patch)},
        )

    kwargs: Dict[str, Any] = {
        "dtype": dtype_object,   # `dtype` is the 4.57 spelling (`torch_dtype` is deprecated)
        "device_map": device_map,
        "trust_remote_code": trust_remote_code,
    }
    if config.attn_implementation:
        kwargs["attn_implementation"] = config.attn_implementation
    if plan.enabled:
        kwargs["quantization_config"] = build_bnb_config(transformers_module, plan, dtype_object)
    try:
        model = model_cls.from_pretrained(config.model_id, **kwargs)
    except VLMRuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified below
        if _is_oom(exc):
            raise VLMRuntimeError(
                f"Out of GPU memory while loading '{config.model_id}' ({dtype_name}, "
                f"{'4-bit' if plan.enabled else 'unquantized'}): {exc}. Enable load_in_4bit, free GPU "
                "memory, or use a larger GPU.",
                code="VLM_INSUFFICIENT_VRAM", details={"cause_type": type(exc).__name__},
            ) from exc
        raise VLMRuntimeError(
            f"Could not load model '{config.model_id}': {exc}",
            code="VLM_LOAD_FAILED", details={"cause_type": type(exc).__name__},
        ) from exc
    model.eval()

    info: Dict[str, Any] = {
        "model_id": config.model_id,
        "family": family.name,
        "loader_class": f"transformers.{family.model_class_name}",
        "dtype": dtype_name,
        "quantization": plan.describe(),
        "device_map": device_map,
        "attn_implementation": config.attn_implementation,
        "trust_remote_code": trust_remote_code,
        "devices": hardware.describe(),
        "image": image_config.describe(),
        "versions": collect_runtime_versions(version_lookup),
        "qwen_vl_utils_matches_baseline":
            package_version("qwen-vl-utils", version_lookup) == QWEN_VL_UTILS_BASELINE,
    }
    if not info["qwen_vl_utils_matches_baseline"]:
        logger.warning(
            "qwen-vl-utils %s differs from the reproducible baseline %s",
            info["versions"].get("qwen-vl-utils"), QWEN_VL_UTILS_BASELINE,
        )
    return LoadedVLM(model=model, processor=processor, family=family, image_config=image_config, runtime_info=info)
