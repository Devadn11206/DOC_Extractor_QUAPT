"""
Execution Client for Streamlit UI: Supports Direct Python Execution and FastAPI HTTP Mode.
"""

from dataclasses import dataclass, field
import hashlib
import io
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid

import httpx

from doc_extractor.config import Settings, load_settings
from doc_extractor.contracts_b import MVPClassificationDomain, MVPDocumentType, ModuleBOutputPayload
from doc_extractor.contracts_c import CanonicalExtractionEnvelope
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.validation import detect_magic_format


@dataclass
class ExecutionResult:
    document_id: str
    filename: str
    sha256: str
    page_count: int
    format_name: str
    pages: Dict[int, bytes]               # page_num -> JPEG/PNG bytes (200 DPI)
    envelope: Dict[str, Any]             # Canonical JSON envelope
    layout_markdown: str                 # Markdown extracted by Module B
    all_blocks: List[Dict[str, Any]]     # All detected raw layout blocks
    execution_time_ms: float
    status: str                          # auto_approved, needs_review, failed
    error_message: Optional[str] = None


class ExtractionClient:
    """Client adapter abstracting Direct Pipeline and FastAPI modes."""

    def __init__(self, mode: str = "Direct", api_base_url: str = "http://localhost:8000") -> None:
        self.mode = mode
        self.api_base_url = api_base_url.rstrip("/")

    def process_document(
        self,
        file_bytes: bytes,
        filename: str,
        document_type_hint: Optional[str] = None,
        tenant_id: str = "tenant_ui",
    ) -> ExecutionResult:
        if self.mode == "FastAPI":
            return self._process_via_fastapi(file_bytes, filename, document_type_hint, tenant_id)
        return self._process_direct(file_bytes, filename, document_type_hint, tenant_id)

    def _process_direct(
        self,
        file_bytes: bytes,
        filename: str,
        document_type_hint: Optional[str],
        tenant_id: str,
    ) -> ExecutionResult:
        t0 = time.perf_counter()
        sha256 = hashlib.sha256(file_bytes).hexdigest()
        detected_fmt = detect_magic_format(file_bytes)
        fmt_str = detected_fmt.value if hasattr(detected_fmt, "value") else str(detected_fmt or "UNKNOWN")

        # 1. Module A Pipeline
        settings = load_settings()
        mod_a = ModuleAPipeline(settings=settings, tenant_id=tenant_id)
        stream = io.BytesIO(file_bytes)
        handoff = mod_a.process_document(stream, filename)

        pages_dict = {}
        for p in handoff.pages:
            try:
                img_path = mod_a.storage.resolve_uri_to_path(p.preprocessed_storage_uri)
                if img_path.exists():
                    pages_dict[p.page_number] = img_path.read_bytes()
            except Exception:
                pass

        # 2. Module B Pipeline
        mod_b = ModuleBPipeline(storage=mod_a.storage, tenant_id=tenant_id)
        mod_b_out = mod_b.process_handoff(handoff)

        # Assemble markdown & blocks
        full_md = "\n\n---\n\n".join(p.markdown_content for p in mod_b_out.pages)
        all_blocks = []
        for p in mod_b_out.pages:
            for b in p.blocks:
                all_blocks.append({
                    "page": p.page_number,
                    "bbox": list(b.bbox_original.to_tuple()) if hasattr(b, "bbox_original") and b.bbox_original else [0, 0, 0, 0],
                    "block_type": b.element_type.value if hasattr(b, "element_type") and hasattr(b.element_type, "value") else "text",
                    "text": getattr(b, "text", ""),
                })

        # 3. Module C Pipeline
        mod_c = ModuleCPipeline()
        envelope = mod_c.process(mod_b_out, force_schema_id=document_type_hint)

        duration_ms = (time.perf_counter() - t0) * 1000.0
        final_status = envelope.document_metadata.status.value if hasattr(envelope.document_metadata.status, "value") else str(envelope.document_metadata.status)

        return ExecutionResult(
            document_id=handoff.document_id,
            filename=filename,
            sha256=sha256,
            page_count=len(handoff.pages),
            format_name=fmt_str,
            pages=pages_dict,
            envelope=envelope.model_dump(),
            layout_markdown=full_md,
            all_blocks=all_blocks,
            execution_time_ms=duration_ms,
            status=final_status,
        )

    def _process_via_fastapi(
        self,
        file_bytes: bytes,
        filename: str,
        document_type_hint: Optional[str],
        tenant_id: str,
    ) -> ExecutionResult:
        t0 = time.perf_counter()
        sha256 = hashlib.sha256(file_bytes).hexdigest()

        with httpx.Client(base_url=self.api_base_url, timeout=30.0) as client:
            files = {"file": (filename, file_bytes)}
            data = {"document_type_hint": document_type_hint or ""}
            headers = {"X-Tenant-ID": tenant_id}

            res = client.post("/api/v1/documents", files=files, data=data, headers=headers)
            if res.status_code not in (200, 202):
                raise RuntimeError(f"API Error {res.status_code}: {res.text}")

            doc_info = res.json()
            doc_id = doc_info.get("document_id")

            # Poll for completion up to 10 seconds
            envelope = None
            final_status = "processing"
            for _ in range(20):
                time.sleep(0.5)
                poll = client.get(f"/api/v1/documents/{doc_id}", headers=headers)
                if poll.status_code == 200:
                    doc_data = poll.json()
                    final_status = doc_data.get("status")
                    if final_status in ("auto_approved", "needs_review", "reviewed", "failed"):
                        envelope = doc_data.get("envelope")
                        break

            # Fetch page images
            pages = {}
            for page_num in range(1, 10):
                page_res = client.get(f"/api/v1/documents/{doc_id}/pages/{page_num}", headers=headers)
                if page_res.status_code == 200:
                    pages[page_num] = page_res.content
                else:
                    break

            duration_ms = (time.perf_counter() - t0) * 1000.0

            return ExecutionResult(
                document_id=doc_id,
                filename=filename,
                sha256=sha256,
                page_count=len(pages) or 1,
                format_name="PDF/Image",
                pages=pages,
                envelope=envelope or {},
                layout_markdown="Markdown retrieved from API",
                all_blocks=[],
                execution_time_ms=duration_ms,
                status=final_status,
            )

