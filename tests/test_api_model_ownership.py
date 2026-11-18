"""
One authoritative type per concept (docs/m5-api-ownership.md).

The API must not carry a competing document model or a second queue abstraction: the durable record of a document is
Module A's ``doc_extractor.models.DocumentRecord`` and HTTP responses use the ``DocumentResource`` DTO mapped from it.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

import ast
import dataclasses
import inspect
import typing
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from doc_extractor import handoff, models
from doc_extractor.api import contracts
from doc_extractor.api.contracts import records
from doc_extractor.api.ports import queue as queue_ports
from doc_extractor.api.ports import repositories
from doc_extractor.api.ports.repositories import DocumentMetadataRepository

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "src" / "doc_extractor" / "api"
DOC = (ROOT / "docs" / "m5-api-ownership.md").read_text(encoding="utf-8")


def _record(**overrides):
    now = datetime.now(timezone.utc).isoformat()
    values = dict(
        document_id="doc_1", tenant_id="tenant_a", file_hash_sha256="a" * 64, claimed_filename="invoice.pdf",
        file_size_bytes=2048, page_count=3, mime_type="application/pdf", status=models.DocumentStatus.NEEDS_REVIEW,
        internal_state=models.IngestionState.READY_FOR_EXTRACTION, created_at=now, updated_at=now,
    )
    values.update(overrides)
    return models.DocumentRecord(**values)


# -- no competing document model -------------------------------------------------------------------

def _classes_defined_in(directory: Path):
    found = {}
    for path in directory.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                found.setdefault(node.name, []).append(str(path.relative_to(ROOT)))
    return found


def test_the_api_package_defines_no_class_named_documentrecord():
    assert "DocumentRecord" not in _classes_defined_in(API_DIR)
    assert not hasattr(records, "DocumentRecord") and not hasattr(contracts, "DocumentRecord")


def test_no_model_or_queue_type_name_is_defined_both_in_the_api_package_and_in_the_engine():
    """Exception classes are excluded on purpose: ``ConflictError`` exists in both hierarchies and is part of the
    still-open error-code decision (D1); this test is about data models and queues."""
    def non_exceptions(found):
        return {name for name in found if not name.endswith("Error")}
    api_classes = non_exceptions(_classes_defined_in(API_DIR))
    engine_classes = non_exceptions({name: where for name, where in _classes_defined_in(ROOT / "src" / "doc_extractor").items()
                                     if any("/api/" not in w for w in where)})
    assert api_classes & engine_classes == set(), "an API type shadows an engine type of the same name"


def test_the_only_remaining_exception_name_clash_is_the_known_conflicterror():
    found = _classes_defined_in(ROOT / "src" / "doc_extractor")
    clashes = {name for name, where in found.items()
               if name.endswith("Error") and any("/api/" in w for w in where) and any("/api/" not in w for w in where)}
    assert clashes == {"ConflictError"}            # error-code vocabulary: PRD decision D1, not changed here


def test_the_repository_port_is_typed_with_module_as_canonical_record_and_enums():
    hints = typing.get_type_hints(DocumentMetadataRepository.get_by_id)
    assert hints["return"] == typing.Optional[models.DocumentRecord]
    assert typing.get_type_hints(DocumentMetadataRepository.create)["record"] is models.DocumentRecord
    update = typing.get_type_hints(DocumentMetadataRepository.update_status)
    assert update["status"] is models.DocumentStatus and update["internal_state"] == typing.Optional[models.IngestionState]
    assert typing.get_type_hints(DocumentMetadataRepository.list_by_tenant)["return"] == typing.List[models.DocumentRecord]
    assert repositories.DocumentRecord is models.DocumentRecord


def test_a_repository_over_modules_record_satisfies_the_port():
    class Repo:
        async def get_by_id(self, tenant_id, document_id): ...
        async def create(self, tenant_id, record): ...
        async def update_status(self, tenant_id, document_id, status, internal_state=None, error=None, page_count=None): ...
        async def list_by_tenant(self, tenant_id, status=None, limit=50, offset=0): ...
        async def delete(self, tenant_id, document_id): ...
    assert isinstance(Repo(), DocumentMetadataRepository)


# -- the response DTO is mapped from the canonical record -------------------------------------------

def test_document_resource_is_mapped_from_modules_record():
    record = _record()
    resource = records.DocumentResource.from_record(record)
    assert (resource.document_id, resource.filename, resource.mime_type, resource.file_size_bytes, resource.page_count) == (
        "doc_1", "invoice.pdf", "application/pdf", 2048, 3)
    assert resource.sha256 == "a" * 64 and resource.status is models.DocumentStatus.NEEDS_REVIEW
    assert resource.created_at == datetime.fromisoformat(record.created_at)
    assert resource.error_code is None and resource.error_message is None


@pytest.mark.parametrize("status", list(models.DocumentStatus))
def test_every_scH_012_status_maps_and_serializes_as_its_value(status):
    payload = records.DocumentResource.from_record(_record(status=status)).model_dump(mode="json")
    assert payload["status"] == status.value


def test_the_response_never_exposes_the_tenant_or_the_internal_state_or_error_details():
    error = {"code": "MALWARE_DETECTED", "message": "File rejected: malware signature detected.", "request_id": "r", "details": {"threat_name": "Eicar"}}
    payload = records.DocumentResource.from_record(_record(status=models.DocumentStatus.REJECTED, error=error)).model_dump(mode="json")
    assert "tenant_id" not in payload and "internal_state" not in payload and "idempotency_key" not in payload
    assert payload["error_code"] == "MALWARE_DETECTED" and payload["error_message"].startswith("File rejected")
    assert "Eicar" not in str(payload) and "details" not in payload             # API-011: code and message only


def test_document_resource_refuses_anything_but_modules_record():
    with pytest.raises(TypeError):
        records.DocumentResource.from_record({"document_id": "doc_1"})
    with pytest.raises(TypeError):
        records.DocumentResource.from_record(object())


def test_document_resource_is_a_frozen_dto_that_forbids_extra_fields():
    resource = records.DocumentResource.from_record(_record())
    with pytest.raises(ValidationError):
        resource.page_count = 9
    with pytest.raises(ValidationError):
        records.DocumentResource(**{**resource.model_dump(), "tenant_id": "tenant_a"})


def test_modules_record_is_still_the_frozen_dataclass_of_module_a():
    assert dataclasses.is_dataclass(models.DocumentRecord) and models.DocumentRecord.__dataclass_params__.frozen


# -- two different kinds of messaging ---------------------------------------------------------------

def test_the_durable_job_message_carries_identifiers_not_a_stage_handoff_or_content():
    fields = records.DocumentProcessingMessage.model_fields
    assert {"document_id", "tenant_id", "event_id", "attempt_count", "max_retries"} <= set(fields)
    assert not any("handoff" in name or "page" in name or "image" in name for name in fields)
    assert records.DocumentProcessingMessage.__doc__ and "NOT Module A's stage hand-off" in records.DocumentProcessingMessage.__doc__


def test_module_as_stage_handoff_queue_is_a_different_in_memory_abstraction():
    assert inspect.isabstract(handoff.QueueAdapter) and handoff.LocalMemoryQueueAdapter.__doc__
    assert set(dir(handoff.QueueAdapter)) & {"publish_message"}
    assert not hasattr(handoff.QueueAdapter, "receive_messages")                    # no lease/ack: not a durable job queue
    assert hasattr(queue_ports.QueueConsumer, "ack") and hasattr(queue_ports.QueueConsumer, "extend_lease")


def test_there_is_exactly_one_queue_protocol_pair_in_the_api():
    protocols = {name for name, where in _classes_defined_in(API_DIR).items() if name.startswith("Queue")}
    assert protocols == {"QueueProducer", "QueueConsumer", "QueueEnvelope"}


# -- the ownership document stays in sync with the code ---------------------------------------------

def test_the_ownership_document_names_every_record_and_queue_type():
    names = {n for n in dir(records) if n.endswith(("Record", "Resource", "Message", "Envelope", "Summary", "Result", "Request", "Response"))
             and getattr(getattr(records, n), "__module__", "") == records.__name__}
    names |= {"QueueProducer", "QueueConsumer", "QueueAdapter", "LocalMemoryQueueAdapter", "ModuleAHandoffPayload", "LifecycleEvent", "CanonicalExtractionEnvelope"}
    missing = sorted(n for n in names if n not in DOC)
    assert not missing, f"docs/m5-api-ownership.md does not mention: {missing}"


def test_the_ownership_document_states_the_team_member_3_dependencies():
    for area in ("Persistence", "Tenant identity", "Authentication", "Queue", "Locking", "Object storage", "Audit"):
        assert area in DOC
