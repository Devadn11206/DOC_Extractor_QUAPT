"""
Client for models served by vLLM (OpenAI-compatible API), for example on a RunPod pod.

A vLLM server serves ONE model, so each role has its own base URL. A role whose variable is unset
keeps running in-process with Transformers.

    VLLM_API_BASE        vision model (Qwen3-VL): page classification and extraction
    VLLM_TEXT_API_BASE   text model (Qwen3-4B-Instruct-2507): OCR text -> schema JSON
    VLLM_OCR_API_BASE    OCR model (GLM-OCR): page image -> text
    VLLM_API_KEY         the value given to ``vllm serve --api-key`` (required for any public host)
    VLLM_TIMEOUT_SECONDS per request, default 120
    VLLM_MAX_RETRIES     extra attempts after a connection failure or HTTP 429/502/503/504, default 2

Document images and text leave this process, so a public host is only accepted over HTTPS with an API key,
redirects are refused, and error messages never carry the request or response body.
"""

import base64
import io
import ipaddress
import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

ROLE_ENV = {"vision": "VLLM_API_BASE", "text": "VLLM_TEXT_API_BASE", "ocr": "VLLM_OCR_API_BASE"}
API_KEY_ENV = "VLLM_API_KEY"
TIMEOUT_ENV = "VLLM_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 120.0
RETRIES_ENV = "VLLM_MAX_RETRIES"
DEFAULT_MAX_RETRIES = 2
RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_BACKOFF_SECONDS = 1.0

Spans = List[Tuple[int, int, float]]


class VLLMError(RuntimeError):
    """A vLLM request that was refused locally or failed."""


def endpoint(role: str) -> Optional[str]:
    """The configured base URL for a role, or None (run the model in-process)."""
    return (os.environ.get(ROLE_ENV[role]) or "").strip() or None


def api_key() -> Optional[str]:
    return (os.environ.get(API_KEY_ENV) or "").strip() or None


def is_private_host(host: str) -> bool:
    """Loopback, private-network addresses and single-label (container / service) names."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith((".local", ".internal"))
    return address.is_loopback or address.is_private


def check_endpoint(api_base: str, key: Optional[str]) -> str:
    """Normalised base URL ending in /v1. Raises if documents or the key would travel unprotected."""
    parts = urllib.parse.urlsplit(api_base.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise VLLMError("vLLM base URL must be an http(s) URL such as http://localhost:8000/v1")
    if not is_private_host(parts.hostname):
        if parts.scheme != "https":
            raise VLLMError(f"vLLM host '{parts.hostname}' is public: use https")
        if not key:
            raise VLLMError(f"vLLM host '{parts.hostname}' is public: set {API_KEY_ENV} (vllm serve --api-key)")
    path = parts.path.rstrip("/") or "/v1"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def image_part(image: Any) -> Dict[str, Any]:
    """A PIL image as an OpenAI-style ``image_url`` content part (inline PNG)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the API key, or the document, to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def chat(
    api_base: str,
    model: str,
    messages: List[Dict[str, Any]],
    max_tokens: int,
    temperature: float = 0.0,
    logprobs: bool = False,
) -> Tuple[str, Spans]:
    """
    One chat completion. Returns (text, spans), where spans are (char_start, char_end, probability) per
    generated token, as ``extractor.attach_field_confidence`` expects. Spans are empty when the server
    sends no usable token probabilities, so no confidence is ever invented.
    """
    key = api_key()
    url = check_endpoint(api_base, key) + "/chat/completions"
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if logprobs:
        body["logprobs"] = True
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    timeout = float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_SECONDS)
    try:
        data = json.loads(_send(request, timeout).decode("utf-8"))
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
    except urllib.error.HTTPError as exc:
        raise VLLMError(f"vLLM request for model '{model}' failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise VLLMError(f"vLLM server for model '{model}' could not be reached: {reason}") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise VLLMError(f"vLLM returned an unreadable response for model '{model}'") from exc
    return text, _token_spans(text, (choice.get("logprobs") or {}).get("content") or [])


def _send(request: urllib.request.Request, timeout: float) -> bytes:
    """
    Sends a request, retrying failures that a moment's wait can fix. A timeout is not retried: the server
    may still be working on the first attempt, and repeating it would double the load.
    """
    retries = max(0, int(os.environ.get(RETRIES_ENV) or DEFAULT_MAX_RETRIES))
    for attempt in range(retries + 1):
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if attempt == retries or exc.code not in RETRY_STATUSES:
                raise
        except (urllib.error.URLError, ConnectionError) as exc:
            if attempt == retries or isinstance(getattr(exc, "reason", exc), TimeoutError):
                raise
        time.sleep(RETRY_BACKOFF_SECONDS * 2 ** attempt)
    raise AssertionError("unreachable")


def _token_spans(text: str, tokens: List[Dict[str, Any]]) -> Spans:
    """Character spans per token. Built from the token bytes: one character (e.g. Devanagari) can span tokens."""
    seen = b""
    start = 0
    spans: Spans = []
    for token in tokens:
        raw = token.get("bytes")
        seen += bytes(raw) if raw is not None else str(token.get("token", "")).encode("utf-8")
        end = len(seen.decode("utf-8", errors="ignore"))
        logprob = token.get("logprob")
        probability = min(1.0, math.exp(logprob)) if isinstance(logprob, (int, float)) else 0.0
        spans.append((start, end, probability))
        start = end
    return spans if seen.decode("utf-8", errors="ignore") == text else []
