from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

JPEG_EXTENSIONS = {".jpg", ".jpeg"}
EXIF_ORIENTATION_TAG = 274


class JpegOptimizationError(RuntimeError):
    """Raised when CPCe-safe JPEG optimization cannot be completed safely."""


@dataclass(frozen=True)
class JpegSnapshot:
    path: str
    filename: str
    format: str
    mode: str
    width: int
    height: int
    exif_orientation: int | None
    file_size: int
    checksum_sha256: str
    decoded_pixels_sha256: str
    exif_sha256: str | None
    icc_sha256: str | None
    progressive: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JpegOptimizationResult:
    image_path: str
    status: str
    eligible: bool
    message: str
    original: JpegSnapshot | None = None
    optimized: JpegSnapshot | None = None
    bytes_saved: int = 0
    percent_reduction: float = 0.0
    output_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_optional(value: bytes | bytearray | memoryview | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(bytes(value)).hexdigest()


def decoded_pixel_checksum(path: Path, block_rows: int = 128) -> str:
    """Decode a JPEG with Pillow and hash its native decoded pixel array.

    Both source and candidate are passed through this exact function, satisfying the
    requirement to compare them with the same decoder. Processing in row blocks avoids
    creating an additional full-image ``tobytes()`` copy.
    """
    digest = hashlib.sha256()
    with Image.open(path) as image:
        if image.format != "JPEG":
            raise JpegOptimizationError(f"{path.name} is not decoded as a JPEG.")
        image.load()
        digest.update(image.mode.encode("ascii", errors="replace"))
        digest.update(f"{image.width}x{image.height}".encode("ascii"))
        for top in range(0, image.height, block_rows):
            bottom = min(image.height, top + block_rows)
            digest.update(image.crop((0, top, image.width, bottom)).tobytes())
    return digest.hexdigest()


def inspect_jpeg(path: Path) -> JpegSnapshot:
    """Validate and inspect a JPEG without applying EXIF auto-orientation."""
    try:
        with Image.open(path) as verify_image:
            if verify_image.format != "JPEG":
                raise JpegOptimizationError(
                    f"The file extension is JPEG-like, but {path.name} is {verify_image.format or 'unknown'}."
                )
            verify_image.verify()

        with Image.open(path) as image:
            if image.format != "JPEG":
                raise JpegOptimizationError(f"{path.name} is not a valid JPEG.")
            width, height = image.size
            mode = image.mode
            exif_bytes = image.info.get("exif")
            icc_bytes = image.info.get("icc_profile")
            progressive = bool(image.info.get("progressive") or image.info.get("progression"))
            try:
                orientation = image.getexif().get(EXIF_ORIENTATION_TAG)
            except (OSError, ValueError, TypeError):
                orientation = None
    except (UnidentifiedImageError, OSError) as exc:
        raise JpegOptimizationError(f"Could not decode {path.name} as a valid JPEG: {exc}") from exc

    return JpegSnapshot(
        path=str(path),
        filename=path.name,
        format="JPEG",
        mode=mode,
        width=width,
        height=height,
        exif_orientation=int(orientation) if orientation is not None else None,
        file_size=path.stat().st_size,
        checksum_sha256=sha256_file(path),
        decoded_pixels_sha256=decoded_pixel_checksum(path),
        exif_sha256=_sha256_optional(exif_bytes),
        icc_sha256=_sha256_optional(icc_bytes),
        progressive=progressive,
    )


def resolve_jpegtran(explicit_path: str | None = None) -> str | None:
    """Find jpegtran from libjpeg-turbo on macOS, Windows, Linux, or Docker."""
    candidates = [
        explicit_path,
        os.environ.get("JPEGTRAN_BIN"),
        shutil.which("jpegtran"),
        shutil.which("jpegtran.exe"),
        str(Path(__file__).resolve().parent / "tools" / "jpegtran"),
        str(Path(__file__).resolve().parent / "tools" / "jpegtran.exe"),
        "/opt/homebrew/opt/jpeg-turbo/bin/jpegtran",
        "/usr/local/opt/jpeg-turbo/bin/jpegtran",
        "/opt/libjpeg-turbo/bin/jpegtran",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def run_jpegtran(
    input_path: Path,
    output_path: Path,
    jpegtran_bin: str,
    timeout_seconds: int = 300,
) -> tuple[int, str]:
    """Run lossless Huffman-table optimization without shell redirection."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output_stream:
        completed = subprocess.run(
            [jpegtran_bin, "-copy", "all", "-optimize", str(input_path)],
            stdout=output_stream,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    return completed.returncode, completed.stderr.decode("utf-8", errors="replace").strip()


def optimize_jpeg_losslessly(
    source_path: Path,
    relative_path: str,
    output_root: Path,
    expected_cpc_filename: str,
    jpegtran_bin: str | None = None,
) -> JpegOptimizationResult:
    """Create a CPCe-safe optimized JPEG or preserve the original on any failure.

    The original file is never overwritten. A candidate is accepted only when dimensions,
    orientation, EXIF, ICC, filename, and decoded pixels all remain unchanged.
    """
    result = JpegOptimizationResult(
        image_path=relative_path,
        status="skipped",
        eligible=False,
        message="JPEG optimization was not attempted.",
    )

    if source_path.suffix.lower() not in JPEG_EXTENSIONS:
        result.message = "Only .jpg and .jpeg images are eligible for CPCe-safe optimization."
        return result

    if expected_cpc_filename != source_path.name:
        result.status = "rejected"
        result.message = "The CPC filename reference does not exactly match the image filename."
        result.errors.append("CPC_FILENAME_MISMATCH")
        return result

    executable = resolve_jpegtran(jpegtran_bin)
    if not executable:
        result.status = "unavailable"
        result.message = "jpegtran from libjpeg-turbo is not available; the original image will be used."
        result.errors.append("JPEGTRAN_NOT_AVAILABLE")
        return result

    try:
        original = inspect_jpeg(source_path)
        result.original = original
        result.eligible = True
    except JpegOptimizationError as exc:
        result.status = "rejected"
        result.message = str(exc)
        result.errors.append("INVALID_JPEG")
        return result

    destination = output_root / Path(relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cpce-jpegtran-") as temp_dir:
        candidate = Path(temp_dir) / source_path.name
        try:
            return_code, stderr = run_jpegtran(source_path, candidate, executable)
        except (OSError, subprocess.SubprocessError) as exc:
            result.status = "rejected"
            result.message = f"jpegtran could not optimize the image: {exc}"
            result.errors.append("JPEGTRAN_FAILED")
            return result

        if return_code != 0 or not candidate.exists() or candidate.stat().st_size == 0:
            result.status = "rejected"
            result.message = "jpegtran returned an error; the original image was preserved."
            result.errors.append("JPEGTRAN_FAILED")
            if stderr:
                result.warnings.append(stderr)
            return result

        try:
            optimized = inspect_jpeg(candidate)
        except JpegOptimizationError as exc:
            result.status = "rejected"
            result.message = f"The optimized candidate is not a valid JPEG: {exc}"
            result.errors.append("OPTIMIZED_JPEG_INVALID")
            return result

        result.optimized = optimized
        failures: list[str] = []
        if optimized.filename != original.filename:
            failures.append("FILENAME_CHANGED")
        if (optimized.width, optimized.height) != (original.width, original.height):
            failures.append("DIMENSIONS_CHANGED")
        if optimized.exif_orientation != original.exif_orientation:
            failures.append("ORIENTATION_CHANGED")
        if optimized.decoded_pixels_sha256 != original.decoded_pixels_sha256:
            failures.append("PIXELS_CHANGED")
        if optimized.exif_sha256 != original.exif_sha256:
            failures.append("EXIF_CHANGED")
        if optimized.icc_sha256 != original.icc_sha256:
            failures.append("ICC_PROFILE_CHANGED")

        if failures:
            result.status = "rejected"
            result.message = "Post-optimization validation failed; the original image was preserved."
            result.errors.extend(failures)
            return result

        if optimized.progressive != original.progressive:
            result.warnings.append(
                "JPEG scan mode changed between baseline and progressive, but dimensions, metadata, "
                "orientation, and decoded pixels remained identical."
            )

        result.bytes_saved = max(0, original.file_size - optimized.file_size)
        result.percent_reduction = (
            round((result.bytes_saved / original.file_size) * 100, 3)
            if original.file_size
            else 0.0
        )

        if optimized.file_size >= original.file_size:
            result.status = "kept_original_no_savings"
            result.message = "The lossless candidate was not smaller, so the original image was preserved."
            return result

        shutil.copy2(candidate, destination)
        if destination.name != source_path.name:
            destination.unlink(missing_ok=True)
            result.status = "rejected"
            result.message = "The output filename changed; the original image was preserved."
            result.errors.append("FILENAME_CHANGED")
            return result

        result.status = "optimized"
        result.message = "CPCe-safe lossless JPEG optimization passed all validation checks."
        result.output_path = str(destination)
        return result
