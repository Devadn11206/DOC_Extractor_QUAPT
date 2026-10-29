"""
Secure Loader for Module A Ingestion Handoff Payloads.

Implements Module B Step 3 requirements:
- Validates ModuleAHandoffPayload using ModuleBInputPayload.
- Rejects unsupported contract versions and invalid processing states.
- Resolves image references exclusively via DocumentStorage.resolve_uri_to_path.
- Enforces tenant isolation and rejects cross-tenant/path-traversal references.
- Confirms referenced files exist, decode cleanly, and match expected dimensions.
- Preserves 1-based page ordering.
- Returns typed, structured errors without leaking secrets or sensitive filesystem paths.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import cv2
import numpy as np

from doc_extractor.contracts_b import ModuleBInputPayload, ModuleAPageRef
from doc_extractor.exceptions import (
    DocumentExtractorError,
    DocumentProcessingError,
    StorageError,
    UnsupportedFileError,
)
from doc_extractor.handoff import ModuleAHandoffPayload
from doc_extractor.storage import StorageBackend


@dataclass(frozen=True)
class LoadedPageImage:
    """A verified, in-memory decoded page image ready for visual parsing."""
    page_number: int
    image_bgr: np.ndarray
    width_px: int
    height_px: int
    dpi: int
    file_path: Path
    page_ref: ModuleAPageRef


@dataclass(frozen=True)
class ModuleBLoadResult:
    """The outcome of securely loading all pages from a Module A handoff."""
    payload: ModuleBInputPayload
    pages: List[LoadedPageImage]
    tenant_id: str
    document_id: str
    run_id: str


class SecureModuleBLoader:
    """
    Secure gateway validating and resolving Module A handoff artifacts
    for consumption by Module B visual parsers and classifiers.
    """

    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage

    def load_handoff(
        self,
        handoff_data: Union[ModuleAHandoffPayload, Dict[str, Any]],
        expected_tenant_id: Optional[str] = None,
    ) -> ModuleBLoadResult:
        """
        Validates the handoff payload and securely decodes all preprocessed page images.

        Raises:
            DocumentProcessingError: If contract validation fails, status is invalid,
                                     or decoded image dimensions do not match metadata.
            StorageError: If path traversal or cross-tenant access is attempted.
            UnsupportedFileError: If referenced image files are missing, empty, or un-decodable.
        """
        # 1. Parse and validate contract schema
        try:
            if isinstance(handoff_data, ModuleAHandoffPayload):
                input_payload = ModuleBInputPayload.from_module_a_payload(handoff_data)
            elif isinstance(handoff_data, dict):
                input_payload = ModuleBInputPayload(**handoff_data)
            else:
                raise DocumentProcessingError(
                    message="Invalid handoff data type: expected ModuleAHandoffPayload or dict.",
                    code="INVALID_PAYLOAD_TYPE",
                )
        except Exception as exc:
            if isinstance(exc, DocumentExtractorError):
                raise
            raise DocumentProcessingError(
                message=f"Module B input validation failed: {str(exc)}",
                code="CONTRACT_VALIDATION_ERROR",
                details={"error": str(exc)},
            ) from exc

        # 2. Enforce tenant boundary
        if expected_tenant_id is not None and input_payload.tenant_id != expected_tenant_id:
            raise StorageError(
                message="Tenant ID mismatch between caller context and document payload.",
                details={
                    "expected_tenant": expected_tenant_id,
                    "payload_tenant": input_payload.tenant_id,
                },
            )

        # 3. Load, decode, and verify each page image
        loaded_pages: List[LoadedPageImage] = []

        for page_ref in input_payload.pages:
            # Resolve URI strictly through StorageBackend abstraction
            # This triggers canonical relative_to path containment and traversal rejection
            try:
                img_path = self.storage.resolve_uri_to_path(page_ref.preprocessed_storage_uri)
            except StorageError:
                raise
            except Exception as exc:
                raise StorageError(
                    message="Failed to resolve preprocessed storage URI safely.",
                    details={"storage_uri": page_ref.preprocessed_storage_uri},
                ) from exc

            # Verify existence
            if not img_path.exists() or not img_path.is_file():
                raise UnsupportedFileError(
                    message=f"Preprocessed page image for page {page_ref.page_number} does not exist in storage.",
                    details={
                        "page_number": page_ref.page_number,
                        "storage_uri": page_ref.preprocessed_storage_uri,
                    },
                )

            # Read and decode image securely using cv2.imdecode
            try:
                with open(img_path, "rb") as f:
                    raw_bytes = f.read()
            except Exception as exc:
                raise StorageError(
                    message=f"Failed to read image bytes for page {page_ref.page_number}.",
                    details={"page_number": page_ref.page_number},
                ) from exc

            if len(raw_bytes) == 0:
                raise UnsupportedFileError(
                    message=f"Preprocessed page image file is empty for page {page_ref.page_number}.",
                    details={"page_number": page_ref.page_number},
                )

            img_bgr = cv2.imdecode(np.frombuffer(raw_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img_bgr is None or img_bgr.size == 0:
                raise UnsupportedFileError(
                    message=f"Corrupted page image: OpenCV failed to decode pixels for page {page_ref.page_number}.",
                    details={"page_number": page_ref.page_number},
                )

            h, w = img_bgr.shape[:2]

            # Verify dimensions against trustworthy Module A handoff metadata
            if w != page_ref.preprocessed_width_px or h != page_ref.preprocessed_height_px:
                raise DocumentProcessingError(
                    message=(
                        f"Image dimension mismatch for page {page_ref.page_number}: "
                        f"metadata claims {page_ref.preprocessed_width_px}x{page_ref.preprocessed_height_px}, "
                        f"actual image is {w}x{h}."
                    ),
                    code="IMAGE_DIMENSION_MISMATCH",
                    details={
                        "page_number": page_ref.page_number,
                        "expected_width": page_ref.preprocessed_width_px,
                        "expected_height": page_ref.preprocessed_height_px,
                        "actual_width": w,
                        "actual_height": h,
                    },
                )

            loaded_pages.append(
                LoadedPageImage(
                    page_number=page_ref.page_number,
                    image_bgr=img_bgr,
                    width_px=w,
                    height_px=h,
                    dpi=page_ref.dpi,
                    file_path=img_path,
                    page_ref=page_ref,
                )
            )

        return ModuleBLoadResult(
            payload=input_payload,
            pages=loaded_pages,
            tenant_id=input_payload.tenant_id,
            document_id=input_payload.document_id,
            run_id=input_payload.run_id,
        )
