"""
Review Queue and Task Workbench Management (API-005, API-016, FR-REV-001..003).
Handles exception routing, four-eyes claiming, field correction submission, and Module D re-validation.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid
from pydantic import BaseModel, Field

from doc_extractor.validation_rules import (
    normalize_clinical_frequency,
    normalize_currency_amount,
    normalize_date_to_iso,
    normalize_phone_number,
    validate_adult_age,
    validate_ifsc,
    validate_pan,
)


class ReviewCorrectionItem(BaseModel):
    field_name: str
    value: Any


class ReviewTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    tenant_id: str
    flagged_fields: List[str] = Field(default_factory=list)
    flag_reasons: Dict[str, str] = Field(default_factory=dict)
    sla_age_seconds: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "unassigned"  # unassigned, claimed, completed
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None


class ReviewTaskManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, ReviewTask] = {}

    def create_task(
        self,
        document_id: str,
        tenant_id: str,
        flagged_fields: List[str],
        flag_reasons: Optional[Dict[str, str]] = None,
    ) -> ReviewTask:
        task = ReviewTask(
            document_id=document_id,
            tenant_id=tenant_id,
            flagged_fields=flagged_fields,
            flag_reasons=flag_reasons or {},
        )
        self._tasks[task.task_id] = task
        return task

    def list_tasks(
        self,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[ReviewTask]:
        tasks = list(self._tasks.values())
        if tenant_id:
            tasks = [t for t in tasks if t.tenant_id == tenant_id]
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def get_task(self, task_id: str) -> Optional[ReviewTask]:
        return self._tasks.get(task_id)

    def claim_task(
        self,
        task_id: str,
        user_id: str,
        user_name: str,
    ) -> tuple:
        task = self.get_task(task_id)
        if not task:
            return None, "NOT_FOUND"
        if task.status == "claimed" and task.assignee_id != user_id:
            return task, "TASK_CLAIMED"
        task.status = "claimed"
        task.assignee_id = user_id
        task.assignee_name = user_name
        return task, None

    def release_task(self, task_id: str, user_id: str) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        if task.status == "claimed" and task.assignee_id == user_id:
            task.status = "unassigned"
            task.assignee_id = None
            task.assignee_name = None
            return True
        return False

    def validate_corrections(self, corrections: List[ReviewCorrectionItem]) -> Dict[str, str]:
        """
        Re-validates edited fields using Module D rules (FR-REV-003).
        Returns a dict of {field_name: error_message} if any fail.
        """
        errors = {}
        for item in corrections:
            k = item.field_name
            v = item.value
            if "pan" in k.lower():
                val, err = validate_pan(str(v) if v else None)
                if err:
                    errors[k] = f"Invalid PAN format ({err})"
            elif "ifsc" in k.lower():
                val, err = validate_ifsc(str(v) if v else None)
                if err:
                    errors[k] = f"Invalid IFSC format ({err})"
            elif "date" in k.lower() or "dob" in k.lower():
                iso, err = normalize_date_to_iso(str(v) if v else None)
                if err:
                    errors[k] = f"Invalid date format ({err})"
            elif "phone" in k.lower() or "mobile" in k.lower():
                e164, err = normalize_phone_number(str(v) if v else None)
                if err:
                    errors[k] = f"Invalid phone format ({err})"
            elif "frequency" in k.lower():
                norm, err = normalize_clinical_frequency(str(v) if v else None)
                if err:
                    errors[k] = f"Unrecognized frequency ({err})"
            elif "amount" in k.lower() or "deposit" in k.lower() or "premium" in k.lower():
                amt, cur, err = normalize_currency_amount(v)
                if err:
                    errors[k] = f"Invalid amount format ({err})"
        return errors
