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
    "jwt": "pyjwt",  # PyJWT is imported as 'jwt' (security extra)
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
        ("api", "requirements-api.txt"), ("security", "requirements-security.txt"),
        ("mongo", "requirements-mongo.txt"), ("queue", "requirements-queue.txt"),
        ("storage", "requirements-storage.txt"), ("test", "requirements-test.txt"),
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


def test_tesseract_is_gone_from_every_dependency_declaration():
    everything = {_name(d) for d in PYPROJECT["dependencies"]} | set(_requirements("requirements.txt"))
    for extra in PYPROJECT["optional-dependencies"].values():
        everything |= {_name(d) for d in extra}
    assert "pytesseract" not in everything
    assert "ocr" not in PYPROJECT["optional-dependencies"]
    assert not (ROOT / "requirements-ocr.txt").exists()


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


# -- MongoDB foundation (Team Member 3, Phase 2A): production and test-only dependencies are separate ------------------

def test_the_mongo_driver_is_a_dedicated_optional_extra_and_never_core():
    assert {_name(d) for d in PYPROJECT["optional-dependencies"]["mongo"]} == {"pymongo"}
    core = {_name(d) for d in PYPROJECT["dependencies"]} | set(_requirements("requirements.txt"))
    assert "pymongo" not in core
    for extra in ("api", "dev", "security", "queue", "test", "ui"):
        assert "pymongo" not in {_name(d) for d in PYPROJECT["optional-dependencies"][extra]}


def test_fakeredis_is_test_only_and_in_no_production_group():
    extras = PYPROJECT["optional-dependencies"]
    # redis is bounded here (test environments only); packaging and tomli are the CI scripts' own imports, declared rather than left transitive
    assert {_name(d) for d in extras["test"]} == {"fakeredis", "redis", "packaging", "tomli"}
    redis_spec = next(d for d in extras["security"] if _name(d) == "redis")
    assert "<" not in redis_spec                                                # production keeps the unbounded floor
    for group in ("security", "api", "mongo", "queue", "ui"):
        assert "fakeredis" not in {_name(d) for d in extras[group]}, group
    core = {_name(d) for d in PYPROJECT["dependencies"]} | set(_requirements("requirements.txt"))
    assert "fakeredis" not in core
    for filename in ("requirements-security.txt", "requirements-api.txt", "requirements-mongo.txt", "requirements-queue.txt"):
        assert "fakeredis" not in _requirements(filename), filename
    assert "fakeredis" in _requirements("requirements-test.txt")


def test_the_mongo_driver_is_imported_only_by_the_mongo_infrastructure():
    importers = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "src").rglob("*.py")
        if any(
            (isinstance(node, ast.Import) and any(a.name.split(".")[0] == "pymongo" for a in node.names))
            or (isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "pymongo")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )
    assert importers and all(name.startswith("src/doc_extractor/infrastructure/mongo/") for name in importers), importers



# -- Phase 2A-7: test-only dependencies stay out of every production group, now and in future ---------------------------------

PRODUCTION_EXTRAS = ("api", "security", "mongo", "queue", "storage", "ui")
TEST_EXTRAS = ("dev", "test")
# A test group may only TIGHTEN a production dependency (the fakeredis-compatible redis cap); nothing else may be shared.
SHARED_WITH_PRODUCTION = {"redis"}
# Well-known test-only packages. They are refused in any production group even before they appear in a test group.
KNOWN_TEST_ONLY = {
    "pytest", "pytest-cov", "pytest-asyncio", "pytest-mock", "pytest-xdist", "pytest-timeout", "pytest-randomly", "pytest-benchmark",
    "coverage", "fakeredis", "mongomock", "freezegun", "time-machine", "hypothesis", "responses", "respx", "requests-mock", "faker",
    "factory-boy", "tox", "nox", "mock", "testcontainers", "moto", "vcrpy",
}
PRODUCTION_REQUIREMENT_FILES = ("requirements.txt", "requirements-api.txt", "requirements-security.txt", "requirements-mongo.txt",
                                "requirements-queue.txt", "requirements-ui.txt")
TEST_REQUIREMENT_FILES = ("requirements-dev.txt", "requirements-test.txt")


def _extra_names(*extras):
    names = set()
    for extra in extras:
        names |= {_name(d) for d in PYPROJECT["optional-dependencies"][extra]}
    return names


def _production_names():
    return {_name(d) for d in PYPROJECT["dependencies"]} | _extra_names(*PRODUCTION_EXTRAS)


def _test_only_names():
    """Declared by a test extra and not part of any production group (redis is shared, with a tighter test bound)."""
    return _extra_names(*TEST_EXTRAS) - SHARED_WITH_PRODUCTION


def test_no_test_only_package_is_in_the_base_installation_or_any_production_extra():
    production = _production_names()
    assert not production & KNOWN_TEST_ONLY, sorted(production & KNOWN_TEST_ONLY)
    assert not production & _test_only_names(), sorted(production & _test_only_names())


def test_no_production_requirements_file_lists_a_test_only_package_or_includes_a_test_file():
    forbidden = KNOWN_TEST_ONLY | _test_only_names()
    for filename in PRODUCTION_REQUIREMENT_FILES:
        listed = set(_requirements(filename))
        assert not listed & forbidden, (filename, sorted(listed & forbidden))
        includes = [line.strip() for line in (ROOT / filename).read_text(encoding="utf-8").splitlines() if line.strip().startswith("-r")]
        assert not [i for i in includes if "requirements-test" in i or "requirements-dev" in i], (filename, includes)


def test_the_test_requirement_files_install_the_core_first_and_nothing_else():
    for filename in TEST_REQUIREMENT_FILES:
        includes = [line.strip() for line in (ROOT / filename).read_text(encoding="utf-8").splitlines() if line.strip().startswith("-r")]
        assert includes == ["-r requirements.txt"], filename


def test_a_package_shared_between_a_test_group_and_production_may_only_be_tightened_by_the_test_group():
    declared = {}
    for extra in (*TEST_EXTRAS, *PRODUCTION_EXTRAS):
        for spec in PYPROJECT["optional-dependencies"][extra]:
            declared.setdefault(_name(spec), {})[extra] = spec
    shared = {name for name, groups in declared.items() if set(groups) & set(TEST_EXTRAS) and set(groups) & set(PRODUCTION_EXTRAS)}
    assert shared == SHARED_WITH_PRODUCTION
    for name in shared:
        production_spec = next(spec for extra, spec in declared[name].items() if extra in PRODUCTION_EXTRAS)
        test_spec = next(spec for extra, spec in declared[name].items() if extra in TEST_EXTRAS)
        assert "<" not in production_spec, production_spec                   # production keeps its bound open
        assert "<" in test_spec, test_spec                                   # the test group alone carries the cap


def test_every_runtime_import_in_src_is_declared_in_a_production_group_not_only_in_a_test_group():
    """A runtime dependency declared only under 'dev' or 'test' would pass every other check and break a production install."""
    production = _production_names()
    not_in_production = {}
    for module, where in _third_party_imports().items():
        dist = DIST_OF_MODULE.get(module, module).lower().replace("_", "-")
        if module in DOCUMENTED_OPTIONAL:
            continue
        if dist not in production:
            not_in_production[module] = where
    assert not_in_production == {}, f"runtime imports not declared in a production group: {not_in_production}"


def _imports_of(directory):
    std = set(sys.stdlib_module_names)
    found = {}
    for path in (ROOT / directory).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            for mod in mods:
                top = mod.split(".")[0]
                if top not in std and top not in FIRST_PARTY and top not in ("tests", "scripts"):
                    found.setdefault(top, str(path.relative_to(ROOT)))
    return found


def test_the_full_test_installation_declares_everything_the_tests_and_ci_scripts_import():
    """``pip install ".[dev,api,security,mongo,queue,test]"`` must hold every third-party package the suite imports (none left to chance)."""
    complete = _declared()                                                   # the base and every extra
    missing = {}
    mapping = {**DIST_OF_MODULE, "bson": "pymongo", "gridfs": "pymongo", "multipart": "python-multipart", "python_multipart": "python-multipart"}
    for directory in ("tests", "scripts"):
        for module, where in _imports_of(directory).items():
            dist = mapping.get(module, module).lower().replace("_", "-")
            if dist not in complete and module not in DOCUMENTED_OPTIONAL:
                missing[module] = where
    assert missing == {}, f"imported by the tests or CI scripts but declared nowhere: {missing}"


def test_the_runner_and_test_only_extras_are_never_imported_by_the_shipped_code():
    imported_by_src = {DIST_OF_MODULE.get(m, m).lower().replace("_", "-") for m in _third_party_imports()}
    leaked = (_test_only_names() | KNOWN_TEST_ONLY) & imported_by_src
    assert leaked == set(), f"src/ imports a test-only package: {sorted(leaked)}"


def test_the_production_install_check_agrees_with_the_project_metadata():
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_production_install", ROOT / "scripts" / "ci" / "check_production_install.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    assert set(check.PRODUCTION_EXTRAS) == set(PRODUCTION_EXTRAS) and set(check.TEST_EXTRAS) == set(TEST_EXTRAS)
    assert set(check.FIRST_PARTY) == set(FIRST_PARTY)
    assert check.declared_only_by_tests(PYPROJECT) == _test_only_names()
    assert set(check.ABSOLUTELY_FORBIDDEN) <= _test_only_names()
    assert set(check.DEFAULT_EXTRAS) <= set(check.PRODUCTION_EXTRAS)


# -- Durable queue (Team Member 3, Phase 3): the AMQP client is a dedicated optional extra imported only by the queue infrastructure ---

def test_the_amqp_client_is_a_dedicated_optional_extra_and_never_core():
    assert {_name(d) for d in PYPROJECT["optional-dependencies"]["queue"]} == {"aio-pika"}
    core = {_name(d) for d in PYPROJECT["dependencies"]} | set(_requirements("requirements.txt"))
    assert "aio-pika" not in core
    for extra in ("api", "dev", "security", "mongo", "test", "ui"):
        assert "aio-pika" not in {_name(d) for d in PYPROJECT["optional-dependencies"][extra]}


def test_the_amqp_client_is_imported_only_by_the_queue_transport():
    importers = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "src").rglob("*.py")
        if any(
            (isinstance(node, ast.Import) and any(a.name.split(".")[0] == "aio_pika" for a in node.names))
            or (isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "aio_pika")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )
    assert importers == ["src/doc_extractor/infrastructure/queue/transport.py"], importers


# -- Object storage (Team Member 3, Phase 4A/4B): boto3 is a dedicated optional extra imported only by storage adapter & KMS ---

def test_the_object_storage_client_is_a_dedicated_optional_extra_and_never_core():
    assert {_name(d) for d in PYPROJECT["optional-dependencies"]["storage"]} == {"boto3", "botocore", "cryptography"}
    core = {_name(d) for d in PYPROJECT["dependencies"]} | set(_requirements("requirements.txt"))
    assert "boto3" not in core and "botocore" not in core
    for extra in ("api", "dev", "mongo", "queue", "test", "ui"):
        assert "boto3" not in {_name(d) for d in PYPROJECT["optional-dependencies"][extra]}
        assert "botocore" not in {_name(d) for d in PYPROJECT["optional-dependencies"][extra]}


def test_the_object_storage_client_is_imported_only_by_the_storage_adapter():
    importers = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "src").rglob("*.py")
        if any(
            (isinstance(node, ast.Import) and any(a.name.split(".")[0] in ("boto3", "botocore") for a in node.names))
            or (isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] in ("boto3", "botocore"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )
    assert importers == [
        "src/doc_extractor/infrastructure/audit/kms_signer.py",
        "src/doc_extractor/infrastructure/audit/sink.py",
        "src/doc_extractor/infrastructure/storage/adapter.py",
        "src/doc_extractor/infrastructure/storage/kms/aws_kms.py",
    ], importers
