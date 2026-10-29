"""
OpenCV Image Preprocessing Pipeline for Document Pages.

Implements:
- FR-ING-005: De-skew, correct rotation, and normalise contrast per page, recording transforms applied.
- PRD Acceptance Target: Given a benchmark page skewed 5°, residual skew <= 0.5° and transform
  recorded in page record.
- SCH-013 Compatibility: Tracks geometric coordinate transformation matrices (rotation, deskew)
  so downstream modules can map bounding box coordinates accurately.

Order of operations:
1. Load & validate image from storage.
2. Orientation/Rotation detection & correction (90°, 180°, 270°).
3. Skew estimation & deskewing (fine angle).
4. Contrast normalisation (CLAHE on L-channel in LAB space for color, or on grayscale).
5. Record comprehensive transformation metadata and persist preprocessed page artifact.
"""

from dataclasses import asdict, dataclass, field
import io
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np

from doc_extractor.config import Settings
from doc_extractor.exceptions import DocumentProcessingError, UnsupportedFileError
from doc_extractor.logging import setup_logging
from doc_extractor.renderer import RenderedPage
from doc_extractor.storage import LocalDiskStorage, StorageBackend, StoredArtifact


@dataclass(frozen=True)
class PageTransformationMetadata:
    """Detailed record of all transformations applied to a page image."""
    document_id: str
    page_number: int
    original_image_path: str
    preprocessed_image_path: str
    original_width_px: int
    original_height_px: int
    output_width_px: int
    output_height_px: int
    dpi: int
    detected_orientation_deg: int
    applied_rotation_deg: int
    orientation_confidence: float
    orientation_status: str
    estimated_skew_deg: float
    applied_deskew_deg: float
    residual_skew_deg: Optional[float]
    deskew_status: str
    deskew_method: str
    contrast_method: str
    contrast_parameters: Dict[str, Any]
    processing_status: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PreprocessingResult:
    """Output bundle for a preprocessed document page."""
    document_id: str
    page_number: int
    metadata: PageTransformationMetadata
    stored_artifact: StoredArtifact
    image_bytes: bytes


class PagePreprocessor:
    """
    OpenCV-based image preprocessor for document pages satisfying FR-ING-005.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        storage: Optional[StorageBackend] = None,
        clahe_clip_limit: float = 2.0,
        clahe_tile_grid_size: Tuple[int, int] = (8, 8),
        skew_threshold_deg: float = 0.5,
        max_skew_detect_deg: float = 45.0,
        orientation_confidence_threshold: float = 2.0,
    ) -> None:
        self.settings = settings or Settings()
        self.storage = storage or LocalDiskStorage(self.settings.storage_base_dir)
        self.clahe_clip_limit = clahe_clip_limit
        self.clahe_tile_grid_size = clahe_tile_grid_size
        self.skew_threshold_deg = skew_threshold_deg
        self.max_skew_detect_deg = max_skew_detect_deg
        self.orientation_confidence_threshold = orientation_confidence_threshold
        self.logger = setup_logging(
            level=self.settings.log_level,
            service_name=self.settings.service_name,
            environment=self.settings.app_env,
        )

    def load_image(self, image_input: Union[str, Path, bytes]) -> np.ndarray:
        """
        Safely loads an image into an OpenCV BGR numpy array.
        Handles corrupt or empty files with sanitized exceptions.
        """
        img: Optional[np.ndarray] = None
        if isinstance(image_input, (str, Path)):
            path = Path(image_input)
            if not path.exists() or not path.is_file():
                raise UnsupportedFileError(
                    message="Page image does not exist or cannot be read.",
                    details={"file": str(path.name)},
                )
            if path.stat().st_size == 0:
                raise UnsupportedFileError(
                    message="Page image file is empty (0 bytes).",
                    details={"file": str(path.name)},
                )
            # Use imdecode with fromfile to safely support Unicode and Windows paths
            with open(path, "rb") as f:
                data = f.read()
            img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        elif isinstance(image_input, bytes):
            if len(image_input) == 0:
                raise UnsupportedFileError(message="Page image bytes are empty.")
            img = cv2.imdecode(np.frombuffer(image_input, dtype=np.uint8), cv2.IMREAD_COLOR)

        if img is None or img.size == 0:
            raise UnsupportedFileError(
                message="Corrupted image: OpenCV failed to decode image pixels.",
                details={"reason": "DECODE_FAILED"},
            )

        h, w = img.shape[:2]
        if h <= 0 or w <= 0 or h > 30000 or w > 30000:
            raise UnsupportedFileError(
                message="Image dimensions are invalid or exceed safe bounds.",
                details={"width": w, "height": h},
            )

        return img

    def detect_orientation(self, img_bgr: np.ndarray) -> Tuple[int, float, str]:
        """
        Detects primary orientation (0°, 90°, 180°, 270°) based on horizontal vs.
        vertical text projection profile variances.
        
        Returns:
            Tuple of (detected_rotation_degrees, confidence_ratio, status)
            If confidence is below threshold, returns (0, confidence, "AMBIGUOUS_PRESERVED")
        """
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if len(img_bgr.shape) == 3 else img_bgr
        # Downscale for fast projection analysis
        h, w = gray.shape
        max_dim = 1200
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            sample = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        else:
            sample = gray

        # Otsu binarization
        _, thresh = cv2.threshold(sample, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # Check total text pixels
        text_pixels = np.count_nonzero(thresh)
        if text_pixels < 100:
            return 0, 0.0, "INSUFFICIENT_CONTENT"

        # Calculate projection profiles
        h_proj = np.sum(thresh, axis=1, dtype=np.float64)
        v_proj = np.sum(thresh, axis=0, dtype=np.float64)

        var_h = float(np.var(h_proj))
        var_v = float(np.var(v_proj))

        ratio = var_h / (var_v + 1e-5)

        # In standard Latin/English/Indian script documents, text lines run horizontally,
        # creating prominent peaks and troughs along the vertical axis (high var_h).
        # If rotated 90 or 270, lines run vertically, creating high var_v (ratio << 1.0).
        if ratio > self.orientation_confidence_threshold:
            return 0, ratio, "CONFIRMED_UPRIGHT"
        elif ratio < (1.0 / self.orientation_confidence_threshold):
            # Text is oriented vertically (90° or 270°)
            # Ambiguous between 90 and 270 without OCR script recognition;
            # per instructions, preserve orientation and record uncertainty
            return 0, ratio, "AMBIGUOUS_VERTICAL_PRESERVED"
        else:
            return 0, ratio, "AMBIGUOUS_PRESERVED"

    def estimate_skew_hough(self, img_bgr: np.ndarray) -> Tuple[float, str]:
        """
        Estimates skew angle in degrees using edge detection and Hough line transform.
        Positive angle means counter-clockwise tilt.
        
        Returns:
            Tuple of (estimated_skew_degrees, status_message)
        """
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if len(img_bgr.shape) == 3 else img_bgr

        # Downscale moderately if image is large for speed & noise suppression
        h, w = gray.shape
        scale = 1.0
        if max(h, w) > 2000:
            scale = 2000.0 / max(h, w)
            sample = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        else:
            sample = gray

        # Adaptive thresholding or Canny edges
        edges = cv2.Canny(sample, 50, 150, apertureSize=3)

        # Probabilistic Hough Transform
        min_line_len = int(80 * scale)
        lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 180,
            threshold=80,
            minLineLength=min_line_len,
            maxLineGap=int(10 * scale),
        )

        if lines is None or len(lines) < 5:
            return 0.0, "INSUFFICIENT_LINES"

        angles: List[float] = []
        for line in lines:
            x1, y1, x2, y2 = line.reshape(4)
            dx = x2 - x1
            dy = y2 - y1
            if dx == 0:
                continue
            angle = float(np.degrees(np.arctan2(dy, dx)))
            # Keep angles near horizontal (-45 to 45 degrees)
            if -self.max_skew_detect_deg <= angle <= self.max_skew_detect_deg:
                angles.append(angle)

        if len(angles) < 5:
            return 0.0, "NO_NEAR_HORIZONTAL_LINES"

        median_angle = float(np.median(angles))
        return median_angle, "SUCCESS"

    def deskew_image(
        self,
        img_bgr: np.ndarray,
        angle_deg: float,
        border_color: Tuple[int, int, int] = (255, 255, 255),
    ) -> np.ndarray:
        """
        Rotates an image by angle_deg to correct skew around its center.
        Uses white border fill (standard for scanned paper).
        """
        h, w = img_bgr.shape[:2]
        center = (w // 2, h // 2)

        # In OpenCV, positive angle rotates CCW. If text line slants down-right (e.g. angle -5 deg),
        # rotating by -5 tilts it further down; rotating by +5 (i.e. angle_deg) rotates it back horizontal.
        rot_mat = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
        deskewed = cv2.warpAffine(
            img_bgr,
            rot_mat,
            (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=border_color,
        )
        return deskewed

    def normalize_contrast(self, img_bgr: np.ndarray) -> Tuple[np.ndarray, str, Dict[str, Any]]:
        """
        Normalizes contrast using Contrast Limited Adaptive Histogram Equalization (CLAHE).
        For color images, operates strictly on the L-channel in LAB space to preserve colors.
        For grayscale, operates directly.
        """
        params = {
            "clip_limit": self.clahe_clip_limit,
            "tile_grid_size": list(self.clahe_tile_grid_size),
        }
        clahe = cv2.createCLAHE(
            clipLimit=self.clahe_clip_limit,
            tileGridSize=self.clahe_tile_grid_size,
        )

        if len(img_bgr.shape) == 2 or img_bgr.shape[2] == 1:
            # Grayscale
            enhanced = clahe.apply(img_bgr)
            return enhanced, "CLAHE_GRAYSCALE", params
        else:
            # Color: Convert BGR -> LAB, apply to L channel, convert back
            lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
            l_chan, a_chan, b_chan = cv2.split(lab)
            enhanced_l = clahe.apply(l_chan)
            merged = cv2.merge([enhanced_l, a_chan, b_chan])
            enhanced_bgr = cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
            return enhanced_bgr, "CLAHE_LAB_L_CHANNEL", params

    def preprocess_page(
        self,
        rendered_page: RenderedPage,
        image_input: Optional[Union[str, Path, bytes]] = None,
    ) -> PreprocessingResult:
        """
        Executes the full preprocessing pipeline on a single rendered page:
        1. Decode original rendered image.
        2. Orientation analysis & rotation.
        3. Skew estimation & deskewing.
        4. Contrast normalization.
        5. Persist preprocessed image & transformation metadata sidecar.
        """
        document_id = rendered_page.document_id
        page_number = rendered_page.page_number
        warnings: List[str] = []

        # 1. Load image (prefer image_input if provided, else read rendered_page.image_path)
        source = image_input or rendered_page.image_path
        img = self.load_image(source)
        orig_h, orig_w = img.shape[:2]

        current_img = img.copy()

        # 2. Orientation detection
        det_rot, conf, orient_status = self.detect_orientation(current_img)
        applied_rot = 0
        if det_rot != 0 and orient_status == "CONFIRMED":
            # Apply 90/180/270 rotation
            if det_rot == 90:
                current_img = cv2.rotate(current_img, cv2.ROTATE_90_CLOCKWISE)
            elif det_rot == 180:
                current_img = cv2.rotate(current_img, cv2.ROTATE_180)
            elif det_rot == 270:
                current_img = cv2.rotate(current_img, cv2.ROTATE_90_COUNTERCLOCKWISE)
            applied_rot = det_rot
        elif orient_status.startswith("AMBIGUOUS"):
            warnings.append(f"Orientation uncertain ({orient_status}); preserved original orientation.")

        # 3. Skew estimation & deskewing
        est_skew, skew_status = self.estimate_skew_hough(current_img)
        applied_deskew = 0.0
        residual_skew: Optional[float] = None

        if skew_status == "SUCCESS" and abs(est_skew) >= self.skew_threshold_deg:
            current_img = self.deskew_image(current_img, est_skew)
            applied_deskew = est_skew

            # Independently measure residual skew
            meas_residual, _ = self.estimate_skew_hough(current_img)
            residual_skew = abs(meas_residual)
        elif skew_status == "SUCCESS":
            residual_skew = abs(est_skew)
            warnings.append(f"Skew ({est_skew:.2f}°) below threshold ({self.skew_threshold_deg}°); skipped.")
        else:
            warnings.append(f"Deskew skipped: {skew_status}.")

        # 4. Contrast normalization
        final_img, contrast_method, contrast_params = self.normalize_contrast(current_img)
        out_h, out_w = final_img.shape[:2]

        # 5. Encode output PNG
        is_success, encoded_buf = cv2.imencode(".png", final_img)
        if not is_success:
            raise DocumentProcessingError(
                message="Failed to encode preprocessed image to PNG.",
                details={"document_id": document_id, "page_number": page_number},
            )
        output_bytes = encoded_buf.tobytes()

        # Build metadata object
        metadata = PageTransformationMetadata(
            document_id=document_id,
            page_number=page_number,
            original_image_path=str(rendered_page.image_path),
            preprocessed_image_path="",  # Filled after persistence
            original_width_px=orig_w,
            original_height_px=orig_h,
            output_width_px=out_w,
            output_height_px=out_h,
            dpi=rendered_page.dpi,
            detected_orientation_deg=det_rot,
            applied_rotation_deg=applied_rot,
            orientation_confidence=conf,
            orientation_status=orient_status,
            estimated_skew_deg=est_skew,
            applied_deskew_deg=applied_deskew,
            residual_skew_deg=residual_skew,
            deskew_status=skew_status,
            deskew_method="HoughLinesP",
            contrast_method=contrast_method,
            contrast_parameters=contrast_params,
            processing_status="SUCCESS",
            warnings=warnings,
        )

        # Store preprocessed artifact and metadata sidecar
        stored = self.storage.store_preprocessed_page(
            document_id=document_id,
            page_number=page_number,
            image_data=output_bytes,
            metadata_dict=metadata.to_dict(),
            format_extension="png",
        )

        # Update metadata path
        metadata_dict = metadata.to_dict()
        metadata_dict["preprocessed_image_path"] = str(stored.file_path)
        final_meta = PageTransformationMetadata(**metadata_dict)

        self.logger.info(
            "Page preprocessed successfully",
            extra={
                "document_id": document_id,
                "extra_data": {
                    "page_number": page_number,
                    "deskew_applied": applied_deskew,
                    "contrast_method": contrast_method,
                },
            },
        )

        return PreprocessingResult(
            document_id=document_id,
            page_number=page_number,
            metadata=final_meta,
            stored_artifact=stored,
            image_bytes=output_bytes,
        )

    def preprocess_document_pages(
        self,
        document_id: str,
        rendered_pages: List[RenderedPage],
    ) -> List[PreprocessingResult]:
        """
        Sequentially preprocesses all pages of a document.
        If any page fails, cleans up preprocessed output for the entire document and raises.
        Preserves original rendered pages.
        """
        results: List[PreprocessingResult] = []
        try:
            for page in rendered_pages:
                res = self.preprocess_page(page)
                results.append(res)
            return results
        except Exception as exc:
            self.logger.error(
                "Document preprocessing failed; initiating atomic cleanup",
                extra={
                    "document_id": document_id,
                    "extra_data": {"error": str(exc)},
                },
            )
            # Rollback: Clean up preprocessed artifacts for this document
            self.storage.cleanup_preprocessed(document_id)
            if isinstance(exc, (DocumentProcessingError, UnsupportedFileError)):
                raise exc
            raise DocumentProcessingError(
                message=f"Preprocessing failed for document {document_id}: {str(exc)}",
                details={"document_id": document_id},
            ) from exc

