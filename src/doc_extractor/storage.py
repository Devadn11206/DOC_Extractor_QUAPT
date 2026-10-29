"""
Storage interfaces and local development implementation.

Adheres to:
- PRD ARC-003: Stages store output under run / document IDs.
- PRD SEC-005: Tenant isolation & encryption extension points.
- Secure path handling: Prevents path traversal, sanitizes filenames, and cleans up partial output.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Any, BinaryIO, List, Optional, Union
import uuid

from doc_extractor.config import Settings
from doc_extractor.exceptions import StorageError


@dataclass(frozen=True)
class StoredArtifact:
    """Metadata representing a persisted file or rendered page image."""
    artifact_id: str
    document_id: str
    page_number: Optional[int]
    file_path: Path
    file_size_bytes: int
    mime_type: str


class StorageBackend(ABC):
    """Abstract interface for document and page image storage."""

    @abstractmethod
    def store_page_image(
        self,
        document_id: str,
        page_number: int,
        image_data: Union[bytes, BinaryIO],
        format_extension: str = "png",
    ) -> StoredArtifact:
        """Stores a rendered page image associated with document_id and page_number."""
        pass

    @abstractmethod
    def store_preprocessed_page(
        self,
        document_id: str,
        page_number: int,
        image_data: Union[bytes, BinaryIO],
        metadata_dict: dict,
        format_extension: str = "png",
    ) -> StoredArtifact:
        """Stores a preprocessed page image and its transformation metadata sidecar."""
        pass

    @abstractmethod
    def get_page_metadata(self, document_id: str, page_number: int) -> Optional[dict]:
        """Loads transformation metadata for a preprocessed page, if available."""
        pass

    @abstractmethod
    def list_page_images(self, document_id: str) -> List[StoredArtifact]:
        """Lists all stored page images for a given document in ascending page order."""
        pass

    @abstractmethod
    def cleanup_preprocessed(self, document_id: str) -> None:
        """Deletes only preprocessed artifacts and metadata for a given document."""
        pass

    @abstractmethod
    def cleanup_document(self, document_id: str) -> None:
        """Deletes all stored artifacts and directories for a given document."""
        pass

    @abstractmethod
    def resolve_uri_to_path(self, storage_uri: str) -> Path:
        """Resolves a logical tenant-relative storage URI to an absolute path safely."""
        pass

    @abstractmethod
    def save_document_record(self, record: Any) -> None:
        """Persists the canonical document record."""
        pass

    @abstractmethod
    def get_document_record(self, document_id: str) -> Optional[Any]:
        """Loads canonical document record by ID."""
        pass

    @abstractmethod
    def find_document_by_hash(self, file_hash: str) -> Optional[Any]:
        """Looks up existing document within the current tenant by SHA-256 hash."""
        pass

    @abstractmethod
    def save_handoff_payload(self, payload: Any) -> None:
        """Persists the terminal Module A handoff payload."""
        pass

    @abstractmethod
    def get_handoff_payload(self, document_id: str) -> Optional[dict]:
        """Retrieves the persisted Module A handoff payload if available."""
        pass


class LocalDiskStorage(StorageBackend):
    """
    Local filesystem storage backend for development and on-premise container storage.
    
    Structure:
    {base_dir}/{tenant_id}/{document_id}/pages/page_{page_number:04d}.png
    
    Guarantees:
    - Path traversal prevention.
    - Document ID isolation.
    - Atomic writes via temporary files to avoid partial reads.
    - Note: In production, backing filesystem should be encrypted with AES-256 (SEC-005).
    """

    def __init__(self, base_dir: Union[str, Path], tenant_id: str = "default_tenant") -> None:
        self.base_dir = Path(base_dir).resolve()
        self.tenant_id = tenant_id

    def _get_document_dir(self, document_id: str) -> Path:
        # Check for path traversal characters before stripping
        if "/" in document_id or "\\" in document_id or ".." in document_id:
            raise StorageError(
                message="Path traversal attempt detected in document storage path.",
                details={"document_id": document_id},
            )
        safe_doc_id = Path(document_id).name
        if not safe_doc_id or safe_doc_id in (".", ".."):
            raise StorageError(
                message="Invalid document ID format.",
                details={"document_id": document_id},
            )
        doc_dir = (self.base_dir / self.tenant_id / safe_doc_id).resolve()
        # Verify that doc_dir remains within base_dir
        if not str(doc_dir).startswith(str(self.base_dir)):
            raise StorageError(
                message="Path traversal attempt detected in document storage path.",
                details={"document_id": document_id},
            )
        return doc_dir

    def store_page_image(
        self,
        document_id: str,
        page_number: int,
        image_data: Union[bytes, BinaryIO],
        format_extension: str = "png",
    ) -> StoredArtifact:
        if page_number < 1:
            raise StorageError(
                message=f"Page number must be one-based (got {page_number}).",
                details={"page_number": page_number},
            )

        doc_dir = self._get_document_dir(document_id)
        pages_dir = doc_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        target_file = pages_dir / f"page_{page_number:04d}.{format_extension}"
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"

        # Write to temporary file in the same directory then atomically rename
        temp_file = pages_dir / f".tmp_{uuid.uuid4().hex}"
        try:
            with open(temp_file, "wb") as f:
                if isinstance(image_data, bytes):
                    f.write(image_data)
                else:
                    image_data.seek(0)
                    shutil.copyfileobj(image_data, f)
            temp_file.replace(target_file)
            file_size = target_file.stat().st_size
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise StorageError(
                message=f"Failed to persist page image: {str(exc)}",
                details={"document_id": document_id, "page_number": page_number},
            ) from exc

        mime_type = f"image/{format_extension}"
        return StoredArtifact(
            artifact_id=artifact_id,
            document_id=document_id,
            page_number=page_number,
            file_path=target_file,
            file_size_bytes=file_size,
            mime_type=mime_type,
        )

    def store_preprocessed_page(
        self,
        document_id: str,
        page_number: int,
        image_data: Union[bytes, BinaryIO],
        metadata_dict: dict,
        format_extension: str = "png",
    ) -> StoredArtifact:
        if page_number < 1:
            raise StorageError(
                message=f"Page number must be one-based (got {page_number}).",
                details={"page_number": page_number},
            )

        doc_dir = self._get_document_dir(document_id)
        prep_dir = doc_dir / "preprocessed"
        prep_dir.mkdir(parents=True, exist_ok=True)

        target_img = prep_dir / f"page_{page_number:04d}_preprocessed.{format_extension}"
        target_meta = prep_dir / f"page_{page_number:04d}_metadata.json"
        artifact_id = f"prep_{uuid.uuid4().hex[:12]}"

        temp_img = prep_dir / f".tmp_img_{uuid.uuid4().hex}"
        temp_meta = prep_dir / f".tmp_meta_{uuid.uuid4().hex}"

        try:
            # Write image
            with open(temp_img, "wb") as f:
                if isinstance(image_data, bytes):
                    f.write(image_data)
                else:
                    image_data.seek(0)
                    shutil.copyfileobj(image_data, f)
            
            # Write JSON metadata
            with open(temp_meta, "w", encoding="utf-8") as f:
                import json
                json.dump(metadata_dict, f, indent=2)

            temp_img.replace(target_img)
            temp_meta.replace(target_meta)
            file_size = target_img.stat().st_size
        except Exception as exc:
            if temp_img.exists():
                temp_img.unlink(missing_ok=True)
            if temp_meta.exists():
                temp_meta.unlink(missing_ok=True)
            raise StorageError(
                message=f"Failed to persist preprocessed page artifact: {str(exc)}",
                details={"document_id": document_id, "page_number": page_number},
            ) from exc

        return StoredArtifact(
            artifact_id=artifact_id,
            document_id=document_id,
            page_number=page_number,
            file_path=target_img,
            file_size_bytes=file_size,
            mime_type=f"image/{format_extension}",
        )

    def get_page_metadata(self, document_id: str, page_number: int) -> Optional[dict]:
        doc_dir = self._get_document_dir(document_id)
        meta_file = doc_dir / "preprocessed" / f"page_{page_number:04d}_metadata.json"
        if not meta_file.exists():
            return None
        try:
            import json
            with open(meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise StorageError(
                message=f"Failed to load transformation metadata: {str(exc)}",
                details={"document_id": document_id, "page_number": page_number},
            ) from exc

    def cleanup_preprocessed(self, document_id: str) -> None:
        doc_dir = self._get_document_dir(document_id)
        prep_dir = doc_dir / "preprocessed"
        if prep_dir.exists():
            shutil.rmtree(prep_dir, ignore_errors=True)

    def list_page_images(self, document_id: str) -> List[StoredArtifact]:
        doc_dir = self._get_document_dir(document_id)
        pages_dir = doc_dir / "pages"
        if not pages_dir.exists():
            return []

        artifacts: List[StoredArtifact] = []
        for file in sorted(pages_dir.glob("page_*.*")):
            if file.is_file() and not file.name.startswith(".tmp_"):
                # Parse page number from page_0001.png
                parts = file.stem.split("_")
                try:
                    p_num = int(parts[1]) if len(parts) > 1 else None
                except ValueError:
                    p_num = None
                
                ext = file.suffix.lstrip(".").lower()
                artifacts.append(
                    StoredArtifact(
                        artifact_id=file.name,
                        document_id=document_id,
                        page_number=p_num,
                        file_path=file,
                        file_size_bytes=file.stat().st_size,
                        mime_type=f"image/{ext}",
                    )
                )
        return artifacts

    def cleanup_document(self, document_id: str) -> None:
        doc_dir = self._get_document_dir(document_id)
        if doc_dir.exists():
            shutil.rmtree(doc_dir, ignore_errors=True)

    def resolve_uri_to_path(self, storage_uri: str) -> Path:
        """
        Safely resolves a logical URI ({tenant_id}/{doc_id}/...) to local filesystem path.
        Guarantees:
        - Prevents directory traversal (e.g. '..', leading slashes).
        - Verifies that target path belongs strictly to self.tenant_id.
        """
        if ".." in storage_uri or "\\" in storage_uri or storage_uri.startswith("/"):
            raise StorageError(
                message="Unsafe storage URI detected.",
                details={"storage_uri": storage_uri},
            )
        resolved = (self.base_dir / storage_uri).resolve()
        expected_tenant_dir = (self.base_dir / self.tenant_id).resolve()
        try:
            resolved.relative_to(expected_tenant_dir)
        except ValueError:
            raise StorageError(
                message="Cross-tenant access or path escape forbidden.",
                details={"storage_uri": storage_uri, "tenant_id": self.tenant_id},
            )
        return resolved

    def save_document_record(self, record: Any) -> None:
        doc_dir = self._get_document_dir(record.document_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        target_file = doc_dir / "document_record.json"
        temp_file = doc_dir / f".tmp_rec_{uuid.uuid4().hex}"

        rec_dict = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                import json
                json.dump(rec_dict, f, indent=2)
            temp_file.replace(target_file)
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise StorageError(
                message=f"Failed to persist document record: {str(exc)}",
                details={"document_id": record.document_id},
            ) from exc

    def get_document_record(self, document_id: str) -> Optional[Any]:
        doc_dir = self._get_document_dir(document_id)
        target_file = doc_dir / "document_record.json"
        if not target_file.exists():
            return None
        try:
            import json
            with open(target_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise StorageError(
                message=f"Failed to read document record: {str(exc)}",
                details={"document_id": document_id},
            ) from exc

    def find_document_by_hash(self, file_hash: str) -> Optional[Any]:
        tenant_dir = (self.base_dir / self.tenant_id).resolve()
        if not tenant_dir.exists():
            return None
        import json
        for doc_dir in tenant_dir.iterdir():
            if doc_dir.is_dir() and doc_dir.name != "idempotency":
                rec_file = doc_dir / "document_record.json"
                if rec_file.exists():
                    try:
                        with open(rec_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if data.get("file_hash_sha256") == file_hash:
                            return data
                    except Exception:
                        continue
        return None

    def save_handoff_payload(self, payload: Any) -> None:
        doc_dir = self._get_document_dir(payload.document_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        target_file = doc_dir / "module_a_handoff.json"
        temp_file = doc_dir / f".tmp_handoff_{uuid.uuid4().hex}"

        payload_dict = payload.to_dict() if hasattr(payload, "to_dict") else dict(payload)
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                import json
                json.dump(payload_dict, f, indent=2)
            temp_file.replace(target_file)
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise StorageError(
                message=f"Failed to persist handoff payload: {str(exc)}",
                details={"document_id": payload.document_id},
            ) from exc

    def get_handoff_payload(self, document_id: str) -> Optional[dict]:
        doc_dir = self._get_document_dir(document_id)
        target_file = doc_dir / "module_a_handoff.json"
        if not target_file.exists():
            return None
        try:
            import json
            with open(target_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise StorageError(
                message=f"Failed to read handoff payload: {str(exc)}",
                details={"document_id": document_id},
            ) from exc

