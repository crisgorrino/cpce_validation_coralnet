from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

JPEG_EXTENSIONS = {".jpg", ".jpeg"}
EXIF_ORIENTATION_TAG = 274


@dataclass
class PreAnnotationResult:
    image_path: str
    status: str
    message: str
    original_width: int | None = None
    original_height: int | None = None
    output_width: int | None = None
    output_height: int | None = None
    original_orientation: int | None = None
    output_orientation: int | None = None
    resized: bool = False
    orientation_normalized: bool = False
    output_path: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_exif_for_normalized_image(image: Image.Image) -> bytes:
    try:
        exif = image.getexif()
        if EXIF_ORIENTATION_TAG in exif:
            exif[EXIF_ORIENTATION_TAG] = 1
        return exif.tobytes()
    except (OSError, ValueError, TypeError):
        return b""


def prepare_unannotated_jpeg(
    source_path: Path,
    relative_path: str,
    output_root: Path,
    max_long_edge: int,
    quality: int = 95,
) -> PreAnnotationResult:
    """Normalize orientation and optionally resize an image with no CPC annotation.

    This intentionally creates a new canonical image and is never called for an image
    matched, or potentially matchable, to an existing CPC file.
    """
    result = PreAnnotationResult(
        image_path=relative_path,
        status="skipped",
        message="Pre-annotation preparation was not attempted.",
    )
    if source_path.suffix.lower() not in JPEG_EXTENSIONS:
        result.message = "Only JPEG images are supported by the optional pre-annotation workflow."
        return result
    if max_long_edge < 256:
        result.status = "error"
        result.message = "Maximum long-edge dimension must be at least 256 pixels."
        return result

    try:
        with Image.open(source_path) as source:
            if source.format != "JPEG":
                result.status = "error"
                result.message = "The file is not a valid JPEG."
                return result
            source.load()
            result.original_width, result.original_height = source.size
            try:
                result.original_orientation = source.getexif().get(EXIF_ORIENTATION_TAG)
            except (OSError, ValueError, TypeError):
                result.original_orientation = None
            icc_profile = source.info.get("icc_profile")
            prepared = ImageOps.exif_transpose(source)
            result.orientation_normalized = result.original_orientation not in (None, 1)
            if max(prepared.size) > max_long_edge:
                prepared.thumbnail((max_long_edge, max_long_edge), Image.Resampling.LANCZOS)
                result.resized = True
            if prepared.mode not in {"RGB", "L", "CMYK"}:
                prepared = prepared.convert("RGB")
            exif_bytes = _safe_exif_for_normalized_image(prepared)
            destination = output_root / Path(relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            save_kwargs: dict[str, Any] = {
                "format": "JPEG",
                "quality": max(1, min(100, quality)),
                "optimize": True,
            }
            if exif_bytes:
                save_kwargs["exif"] = exif_bytes
            if icc_profile:
                save_kwargs["icc_profile"] = icc_profile
            prepared.save(destination, **save_kwargs)
            result.output_width, result.output_height = prepared.size
            result.output_orientation = 1
            result.output_path = str(destination)
            result.status = "prepared"
            result.message = (
                "Created a new pre-annotation canonical JPEG. This output must be used before "
                "new CPCe annotation points are created."
            )
            return result
    except (UnidentifiedImageError, OSError) as exc:
        result.status = "error"
        result.message = f"Could not prepare the image: {exc}"
        return result
