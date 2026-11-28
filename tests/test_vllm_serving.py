"""Models served by vLLM, exercised against a real local HTTP server that speaks the OpenAI protocol."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import math
from pathlib import Path
import threading

import pytest
from PIL import Image

from doc_extractor import vllm_client
from doc_extractor.adapters import OCRTextAdapter
from doc_extractor.config import Settings
from doc_extractor.extractor import SchemaGuidedExtractor
from tests.test_local_qwen_integration import mock_module_b_doc01_payload  # noqa: F401  (fixture)

KEY = "test-key"


@pytest.fixture
def server(monkeypatch):
    """Answers with the queued replies; records every request."""
    state = {"requests": [], "replies": [], "status": 200, "statuses": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            status = state["statuses"].pop(0) if state["statuses"] else state["status"]
            if status != 200:
                self.send_response(status)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.end_headers()
                return
            text = state["replies"].pop(0)
            choice = {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
            if body.get("logprobs"):
                choice["logprobs"] = {"content": [
                    {"token": c, "bytes": list(c.encode()), "logprob": math.log(0.9)} for c in text]}
            payload = json.dumps({
                "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [choice], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    monkeypatch.setenv("VLLM_API_KEY", KEY)
    monkeypatch.setattr(vllm_client, "RETRY_BACKOFF_SECONDS", 0)
    for name in vllm_client.ROLE_ENV.values():
        monkeypatch.delenv(name, raising=False)
    yield state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def page(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (40, 40), "white").save(path)
    return str(path)


def _no_local_load(*args, **kwargs):
    raise AssertionError("a model was loaded in-process although a vLLM server is configured")


def test_page_extraction_uses_vllm_and_keeps_token_confidence(server, page, monkeypatch):
    monkeypatch.setenv("VLLM_API_BASE", server["url"])
    monkeypatch.setattr(SchemaGuidedExtractor, "_load_model", _no_local_load)
    server["replies"] = ['```json\n{"name": {"value": "RAVI", "bbox": [1, 2, 3, 4]}}\n```']
    extractor = SchemaGuidedExtractor(model_name="Qwen/Qwen3-VL-4B-Instruct", use_llm=True)

    raw, prov = extractor.extract_page_image(page, None, system_prompt="PROMPT")

    assert raw["name"]["value"] == "RAVI" and raw["name"]["confidence"] == pytest.approx(0.9)
    assert prov.provider == "vllm_openai_api"
    sent = server["requests"][0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == f"Bearer {KEY}"
    assert sent["body"]["model"] == "Qwen/Qwen3-VL-4B-Instruct"
    assert sent["body"]["messages"][0]["content"] == "PROMPT"
    assert sent["body"]["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_classification_uses_vllm(server, page, monkeypatch):
    monkeypatch.setenv("VLLM_API_BASE", server["url"])
    monkeypatch.setattr(SchemaGuidedExtractor, "_load_model", _no_local_load)
    server["replies"] = ['{"category": "structured"}']
    result = SchemaGuidedExtractor(use_llm=True).classify_page_image(page)
    assert result == {"category": "structured"} and server["requests"][0]["body"]["max_tokens"] == 128


def test_ocr_text_route_uses_both_vllm_servers(server, page, monkeypatch):
    monkeypatch.setenv("VLLM_OCR_API_BASE", server["url"])
    monkeypatch.setenv("VLLM_TEXT_API_BASE", server["url"])
    monkeypatch.setattr(OCRTextAdapter, "_load_ocr", _no_local_load)
    monkeypatch.setattr(OCRTextAdapter, "_load_text", _no_local_load)
    server["replies"] = ["Gold Shield Plan", '{"title": {"value": "Gold Shield Plan", "bbox": null}}']

    raw, prov, _ = OCRTextAdapter().extract(page, "PROMPT")

    assert raw["title"]["value"] == "Gold Shield Plan" and raw["title"]["confidence"] == pytest.approx(0.9)
    assert [r["body"]["model"] for r in server["requests"]] == ["zai-org/GLM-OCR", "Qwen/Qwen3-4B-Instruct-2507"]
    assert prov.provider == "vllm_ocr_text"


def test_settings_path_reaches_vllm_through_litellm(server, mock_module_b_doc01_payload):
    server["replies"] = [json.dumps({"applicant": {"full_name": "ARUN VERMA"},
                                     "account_details": {"account_type": "SAVINGS"}})]
    settings = Settings(llm_api_base=server["url"], llm_use_inference=True)

    result = SchemaGuidedExtractor.from_settings(settings).extract_with_provenance(mock_module_b_doc01_payload)

    assert result.payload.applicant.full_name == "ARUN VERMA" and result.provenance.provider == "vllm"
    sent = server["requests"][0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == f"Bearer {KEY}"
    assert sent["body"]["model"] == "Qwen/Qwen3-4B-Instruct-2507"


@pytest.mark.parametrize("status", [401, 302])
def test_failures_and_redirects_raise_without_leaking_the_request(server, status):
    server["status"] = status
    with pytest.raises(vllm_client.VLLMError) as exc:
        vllm_client.chat(server["url"], "m", [{"role": "user", "content": "SECRET"}], 5)
    assert str(status) in str(exc.value) and "SECRET" not in str(exc.value) and KEY not in str(exc.value)
    assert len(server["requests"]) == 1


@pytest.mark.parametrize("url,key", [
    ("http://abc-8000.proxy.runpod.net/v1", "k"),       # public without TLS
    ("https://abc-8000.proxy.runpod.net/v1", None),     # public without a key
    ("ftp://localhost/v1", "k"),
])
def test_unprotected_endpoints_are_refused(url, key):
    with pytest.raises(vllm_client.VLLMError):
        vllm_client.check_endpoint(url, key)


@pytest.mark.parametrize("url,expected", [
    ("https://abc-8000.proxy.runpod.net", "https://abc-8000.proxy.runpod.net/v1"),
    ("http://localhost:8000/v1/", "http://localhost:8000/v1"),
    ("http://vllm:8000/v1", "http://vllm:8000/v1"),
    ("http://10.0.0.5:8000/v1", "http://10.0.0.5:8000/v1"),
])
def test_accepted_endpoints_are_normalised(url, expected):
    assert vllm_client.check_endpoint(url, "k") == expected


def test_public_http_is_rejected_by_settings():
    with pytest.raises(ValueError, match="use https"):
        Settings(llm_api_base="http://abc-8000.proxy.runpod.net/v1")


def test_a_generic_openai_variable_does_not_redirect_documents(monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    monkeypatch.delenv("VLLM_API_BASE", raising=False)
    assert SchemaGuidedExtractor()._vllm_vision_base() is None


def test_probabilities_are_dropped_when_tokens_do_not_rebuild_the_text():
    assert vllm_client._token_spans("abc", [{"token": "ab", "logprob": 0.0}]) == []
    split = list("\u0905".encode())                       # one character delivered as two byte tokens
    spans = vllm_client._token_spans("\u0905", [{"bytes": split[:1], "logprob": 0.0},
                                                {"bytes": split[1:], "logprob": 0.0}])
    assert spans[-1][1] == 1


def _ask(server):
    return vllm_client.chat(server["url"], "m", [{"role": "user", "content": "x"}], 5)[0]


def test_a_temporary_server_error_is_retried(server):
    server["statuses"], server["replies"] = [503, 429], ["done"]
    assert _ask(server) == "done" and len(server["requests"]) == 3


@pytest.mark.parametrize("status,attempts", [(503, 3), (400, 1), (524, 1)])
def test_retries_are_bounded_and_limited_to_temporary_errors(server, status, attempts):
    server["status"] = status
    with pytest.raises(vllm_client.VLLMError):
        _ask(server)
    assert len(server["requests"]) == attempts


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "serve_vllm", Path(__file__).resolve().parents[1] / "scripts" / "serve_vllm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("gpus,placement,quantized", [
    (1, [0, 0, 0], [True, True, False]),
    (2, [0, 1, 1], [False, True, False]),
])
def test_the_launcher_places_the_models_on_the_gpus(gpus, placement, quantized):
    launcher = _launcher()
    servers = launcher.plan(launcher.parse_args([]), gpus)
    assert [s.gpu for s in servers] == placement and [s.quantize for s in servers] == quantized
    assert [s.port for s in servers] == [8000, 8001, 8002]
    for gpu in set(placement):
        assert sum(s.memory for s in servers if s.gpu == gpu) <= 0.95


def test_the_launcher_command_never_carries_the_key_and_uses_float16_for_a_t4(monkeypatch):
    launcher = _launcher()
    monkeypatch.setenv("VLLM_API_KEY", KEY)
    args = launcher.parse_args([])
    cmd = launcher.command(launcher.plan(args, 2)[1], args, launcher.lacks_bfloat16("Tesla T4"))
    assert KEY not in cmd and cmd[cmd.index("--dtype") + 1] == "float16"
    assert not launcher.lacks_bfloat16("NVIDIA RTX A5000") and not launcher.lacks_bfloat16("NVIDIA L4")


def test_the_launcher_refuses_a_network_address_without_a_key(monkeypatch):
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    assert _launcher().main(["--host", "0.0.0.0", "--dry-run"]) == 2


def test_the_text_server_context_fits_the_longest_request_the_engine_makes():
    launcher = _launcher()
    text = launcher.plan(launcher.parse_args([]), 2)[1]
    assert text.max_model_len >= OCRTextAdapter().max_json_tokens + 8000

