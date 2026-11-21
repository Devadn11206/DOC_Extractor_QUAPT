"""
Phase 1 / Task 8A (D5): packaging metadata is consistent and complete.

- pyproject.toml core dependencies == requirements.txt (same distributions, same specifiers);
  the extras are mirrored by requirements-dev/-ui/-vlm.txt.
- Every third-party module imported anywhere in ``src/`` is a declared dependency (core or extra)
  or one of the explicitly documented optional/legacy imports; nothing relies on a transitive
  install (e.g. ``jsonschema`` used to come in only through litellm).
- Heavy VLM packages and PyTorch stay out of the core dependencies.
- The Python-version claim is internally consistent.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib" if sys.version_info >= (3, 11) else "tomli")

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

# Packages shipped by this distribution (src/): never third-party. engine_errors is the shared error-base leaf.
FIRST_PARTY = ("doc_extractor", "schema_lifecycle", "engine_errors")

# import name -> distribution name (only where they differ)
DIST_OF_MODULE = {
    "PIL": "pillow",
    "cv2": "opencv-python-headless",
    "yaml": "pyyaml",
}
# Imported lazily by optional/legacy code paths; deliberately NOT declared (see README):
#   torch              installed separately per platform (CUDA/CPU/MPS), needed only for local Qwen-VL
#   rapidocr_onnxruntime  legacy notebook-era casa_reader.py only
DOCUMENTED_OPTIONAL = {"torch", "rapidocr_onnxruntime"}
HEAVY_VLM = {"torch", "transformers", "accelerate", "bitsandbytes", "qwen-vl-utils", "qwen_vl_utils"}


def _name(spec: str) -> str:
    return re.split(r"[<>=!~\[; ]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _requirements(path: str):
    out = {}
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            out[_name(line)] = line.replace(" ", "")
    return out


def _declared():
    names = {_name(d) for d in PYPROJECT["dependencies"]}
    for extra in PYPROJECT["optional-dependencies"].values():
        names |= {_name(d) for d in extra}
    return names


def _third_party_imports():
    std = set(sys.stdlib_module_names)
    found = {}
    for path in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            for mod in mods:
                top = mod.split(".")[0]
                if top not in std and top not in FIRST_PARTY:
                    found.setdefault(top, str(path.relative_to(ROOT)))
    return found


def test_core_dependencies_match_between_pyproject_and_requirements_txt():
    pyproject_core = {_name(d): d.replace(" ", "") for d in PYPROJECT["dependencies"]}
    assert _requirements("requirements.txt") == pyproject_core


def test_extras_are_mirrored_by_their_requirements_files():
    extras = PYPROJECT["optional-dependencies"]
    for extra, filename in (
        ("dev", "requirements-dev.txt"), ("ui", "requirements-ui.txt"),
        ("ocr", "requirements-ocr.txt"), ("api", "requirements-api.txt"),
    ):
        declared = {_name(d): d.replace(" ", "") for d in extras[extra]}
        listed = {k: v for k, v in _requirements(filename).items() if k not in _requirements("requirements.txt")}
        assert listed == declared, f"{filename} differs from the '{extra}' extra"


def test_every_third_party_import_in_src_is_declared_or_documented_optional():
    declared = _declared()
    undeclared = {}
    for module, where in _third_party_imports().items():
        dist = DIST_OF_MODULE.get(module, module).lower().replace("_", "-")
        if dist not in declared and module not in DOCUMENTED_OPTIONAL:
            undeclared[module] = where
    assert undeclared == {}, f"undeclared third-party imports: {undeclared}"


def test_jsonschema_is_a_direct_core_dependency():
    assert "jsonschema" in {_name(d) for d in PYPROJECT["dependencies"]}
    assert "jsonschema" in _requirements("requirements.txt")


def test_multimodal_stack_is_core_per_prd_v15_section_12_1():
    """PRD v1.5 12.1 lists transformers, accelerate, bitsandbytes and qwen-vl-utils as core dependencies."""
    core = {_name(d) for d in PYPROJECT["dependencies"]}
    assert {"transformers", "accelerate", "bitsandbytes", "qwen-vl-utils"} <= core
    assert {"transformers", "accelerate", "bitsandbytes", "qwen-vl-utils"} <= set(_requirements("requirements.txt"))
    # PyTorch/torchvision are platform specific (CUDA/CPU/MPS): installed separately, never pinned here,
    # so a Kaggle/Colab image's matching pair is never reinstalled over.
    assert not core & {"torch", "torchvision"}
    assert "vlm" not in PYPROJECT["optional-dependencies"]


def test_tesseract_is_optional_and_not_in_core_or_requirements_txt():
    core = {_name(d) for d in PYPROJECT["dependencies"]} | set(_requirements("requirements.txt"))
    assert "pytesseract" not in core
    assert "pytesseract" in {_name(d) for d in PYPROJECT["optional-dependencies"]["ocr"]}


def test_bitsandbytes_is_skipped_on_macos_where_it_has_no_gpu_path():
    spec = next(d for d in PYPROJECT["dependencies"] if _name(d) == "bitsandbytes")
    assert "darwin" in spec and "sys_platform" in spec


def test_gradio_is_an_optional_ui_extra_not_a_core_dependency():
    assert "gradio" in {_name(d) for d in PYPROJECT["optional-dependencies"]["ui"]}
    assert "gradio" not in {_name(d) for d in PYPROJECT["dependencies"]}
    assert "gradio" not in _requirements("requirements.txt")


def test_qwen3_vl_transformers_floor_is_not_below_4_57():
    spec = next(d for d in PYPROJECT["dependencies"] if _name(d) == "transformers")
    major, minor, *_ = (int(x) for x in re.search(r">=\s*([\d.]+)", spec).group(1).split("."))
    assert (major, minor) >= (4, 57)


def test_core_matches_the_reproducible_gpu_baseline():
    core = {_name(d): d.replace(" ", "") for d in PYPROJECT["dependencies"]}
    assert core["qwen-vl-utils"] == "qwen-vl-utils==0.0.14"          # exact baseline pin


# -- REST API foundation: a dedicated optional group, nothing added to core ------------------------

API_STACK = {"fastapi", "starlette", "anyio", "uvicorn", "python-multipart"}


def test_api_stack_is_a_dedicated_optional_extra_and_never_core():
    api_extra = {_name(d) for d in PYPROJECT["optional-dependencies"]["api"]}
    assert api_extra == API_STACK
    core = {_name(d) for d in PYPROJECT["dependencies"]} | set(_requirements("requirements.txt"))
    assert not core & (API_STACK | {"httpx"})


def test_httpx_is_a_development_dependency_only():
    assert "httpx" in {_name(d) for d in PYPROJECT["optional-dependencies"]["dev"]}
    assert "httpx" in _requirements("requirements-dev.txt")
    assert "httpx" not in {_name(d) for d in PYPROJECT["optional-dependencies"]["api"]}
    assert "httpx" not in _requirements("requirements-api.txt")


def test_requirements_api_installs_core_first_and_lists_only_the_api_extra():
    text = (ROOT / "requirements-api.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in text
    assert set(_requirements("requirements-api.txt")) - set(_requirements("requirements.txt")) == API_STACK


def test_the_api_extra_leaves_the_existing_groups_unchanged():
    extras = PYPROJECT["optional-dependencies"]
    assert {_name(d) for d in extras["ui"]} == {"gradio"}
    assert {_name(d) for d in extras["ocr"]} == {"pytesseract"}
    assert {_name(d) for d in extras["dev"]} == {"pytest", "pytest-cov", "httpx"}


def test_every_first_party_package_in_src_is_declared_as_first_party_and_vice_versa():
    """A new top-level package under src/ must be listed in FIRST_PARTY, or its imports would count as third-party."""
    packages = {p.name for p in (ROOT / "src").iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert packages == set(FIRST_PARTY)


def test_package_discovery_ships_every_first_party_package():
    """setuptools finds packages under src/ (``where = ["src"]``) with no include/exclude filter, so the new leaf package
    ``engine_errors`` is installed with the others. (Checked from the metadata: it must not need setuptools to run.)"""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["packages"]["find"]
    assert config == {"where": ["src"]}
    discovered = {p.name for p in (ROOT / "src").iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert discovered == set(FIRST_PARTY) and "engine_errors" in discovered
