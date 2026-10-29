"""
Module A Handoff Contracts and Coordinate Inversion Utilities.

Defines:
- ModuleAHandoffPayload: Versioned JSON-serializable envelope handed off to Module B (ARC-003, SCH-011).
- PageHandoffRecord: Ordered page references (original 200-DPI & preprocessed) and transformation metadata.
- CoordinateTransform: Inversion functions to map preprocessed-space bounding boxes ([x0, y0, x1, y1])
  back to original 200-DPI page space (SCH-013).
- QueueAdapter: Abstract queue interface for durable inter-stage messaging (ARC-003).
"""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np

from doc_extractor.models import DocumentStatus, IngestionState


MODULE_A_CONTRACT_VERSION = "1.0.0"


@dataclass(frozen=True)
class PageHandoffRecord:
    """Handoff representation of an individual document page."""
    page_number: int  # 1-based original sequence
    original_artifact_id: str
    preprocessed_artifact_id: str
    original_storage_uri: str
    preprocessed_storage_uri: str
    original_width_px: int
    original_height_px: int
    preprocessed_width_px: int
    preprocessed_height_px: int
    dpi: int  # Fixed at 200 (SCH-013)
    applied_rotation_deg: int
    applied_deskew_deg: float
    residual_skew_deg: Optional[float]
    contrast_method: str
    metadata_storage_uri: str


@dataclass(frozen=True)
class ModuleAHandoffPayload:
    """
    Standardized, versioned JSON payload passed from Module A to Module B (ARC-003).
    """
    contract_version: str
    run_id: str
    document_id: str
    tenant_id: str
    status: str
    internal_state: str
    claimed_filename: str
    file_hash_sha256: str
    file_size_bytes: int
    page_count: int
    mime_type: str
    pages: List[PageHandoffRecord]
    created_at: str
    completed_at: str
    error: Optional[Dict[str, Any]] = None
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModuleAHandoffPayload":
        pages_data = data.get("pages", [])
        pages = [
            PageHandoffRecord(**p) if isinstance(p, dict) else p
            for p in pages_data
        ]
        clean_data = dict(data)
        clean_data["pages"] = pages
        return cls(**clean_data)


class CoordinateTransform:
    """
    Coordinate mapping utilities adhering to PRD SCH-013:
    Downstream bounding boxes [x0, y0, x1, y1] are defined in pixels of the 200-DPI page image.
    When vision/OCR models detect text on preprocessed images, this class maps those bounding
    boxes back into the original 200-DPI coordinate space with origin top-left.
    """

    @staticmethod
    def map_bbox_preprocessed_to_original(
        bbox: Tuple[float, float, float, float],
        original_width: int,
        original_height: int,
        applied_rotation_deg: int = 0,
        applied_deskew_deg: float = 0.0,
    ) -> Tuple[float, float, float, float]:
        """
        Maps a bounding box [x0, y0, x1, y1] from preprocessed image space
        back to original 200-DPI page image space.
        
        Order of forward operations was:
        1. Orientation rotation (cv2.rotate 90/180/270).
        2. Deskew affine rotation around center of rotated image.
        
        Therefore, inverse transformation is:
        1. Invert deskew affine rotation around center of post-orientation image.
        2. Invert orientation rotation (cv2.rotate inverse mapping).
        """
        x0, y0, x1, y1 = bbox
        corners = np.array([
            [x0, y0],
            [x1, y0],
            [x1, y1],
            [x0, y1],
        ], dtype=np.float32)

        # Post-orientation dimensions
        if applied_rotation_deg in (90, 270):
            rot_w = float(original_height)
            rot_h = float(original_width)
        else:
            rot_w = float(original_width)
            rot_h = float(original_height)

        # 1. Invert deskew rotation around center of the post-orientation image
        if abs(applied_deskew_deg) > 1e-4:
            center = (rot_w / 2.0, rot_h / 2.0)
            # Forward deskew used cv2.getRotationMatrix2D(center, applied_deskew, 1.0)
            # Invert using cv2.invertAffineTransform
            fwd_mat = cv2.getRotationMatrix2D(center, applied_deskew_deg, 1.0)
            inv_rot_mat = cv2.invertAffineTransform(fwd_mat)
            ones = np.ones(shape=(len(corners), 1))
            pts_h = np.hstack([corners, ones])
            corners = inv_rot_mat.dot(pts_h.T).T

        # 2. Invert orientation rotation (90°, 180°, 270°)
        # cv2.ROTATE_90_CLOCKWISE maps: (x, y) -> (rot_w - 1 - y, x)
        # Inverse (ROTATE_90_COUNTERCLOCKWISE) maps: (x', y') -> (y', rot_w - 1 - x')
        if applied_rotation_deg == 90:
            inv_pts = np.zeros_like(corners)
            inv_pts[:, 0] = corners[:, 1]
            inv_pts[:, 1] = rot_w - 1.0 - corners[:, 0]
            corners = inv_pts
        elif applied_rotation_deg == 180:
            inv_pts = np.zeros_like(corners)
            inv_pts[:, 0] = rot_w - 1.0 - corners[:, 0]
            inv_pts[:, 1] = rot_h - 1.0 - corners[:, 1]
            corners = inv_pts
        elif applied_rotation_deg == 270:
            inv_pts = np.zeros_like(corners)
            inv_pts[:, 0] = rot_h - 1.0 - corners[:, 1]
            inv_pts[:, 1] = corners[:, 0]
            corners = inv_pts

        # Enclosing axis-aligned bounding box
        min_x = float(np.min(corners[:, 0]))
        min_y = float(np.min(corners[:, 1]))
        max_x = float(np.max(corners[:, 0]))
        max_y = float(np.max(corners[:, 1]))

        # Clamp to original image boundaries [0, original_width] x [0, original_height]
        clamped_x0 = max(0.0, min(float(original_width), min_x))
        clamped_y0 = max(0.0, min(float(original_height), min_y))
        clamped_x1 = max(0.0, min(float(original_width), max_x))
        clamped_y1 = max(0.0, min(float(original_height), max_y))

        return (
            round(clamped_x0, 2),
            round(clamped_y0, 2),
            round(clamped_x1, 2),
            round(clamped_y1, 2),
        )


class QueueAdapter(ABC):
    """
    Abstract interface for queue brokers connecting pipeline stages (ARC-003).
    Stages communicate through durable messages stored under run_id.
    """

    @abstractmethod
    def publish_message(self, topic: str, payload: ModuleAHandoffPayload) -> str:
        """Publishes a handoff payload to the queue topic. Returns message_id."""
        pass


class LocalMemoryQueueAdapter(QueueAdapter):
    """
    In-memory queue adapter for local testing and module handoff verification.
    """

    def __init__(self) -> None:
        self.published_messages: List[Tuple[str, ModuleAHandoffPayload]] = []

    def publish_message(self, topic: str, payload: ModuleAHandoffPayload) -> str:
        import uuid
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        self.published_messages.append((topic, payload))
        return msg_id

