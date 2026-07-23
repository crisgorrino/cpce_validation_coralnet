from __future__ import annotations

import copy
import csv
import difflib
import errno
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path, PureWindowsPath
from typing import Any

from PIL import Image, UnidentifiedImageError

from cpc_parser import CpcFile, CpcParseError
from jpeg_optimizer import (
    JpegOptimizationResult,
    optimize_jpeg_losslessly,
    resolve_jpegtran,
    sha256_file,
)
from preannotation import PreAnnotationResult, prepare_unannotated_jpeg

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
LABEL_COLUMN_CANDIDATES = (
    "short code",
    "short_code",
    "label code",
    "label_code",
    "code",
)
SYSTEM_METADATA_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

CANNOT_SAFELY_REPAIR_CODES = {
    "CPC_PARSE_ERROR",
    "INVALID_CPC_DIMENSIONS",
    "NON_INTEGER_SCALE_FACTOR",
    "INVALID_POINT_X",
    "INVALID_POINT_Y",
    "POINT_OUTSIDE_IMAGE",
    "MULTIPLE_CPCS_FOR_IMAGE",
}
NEEDS_REVIEW_CODES = {
    "IMAGE_NOT_FOUND",
    "AMBIGUOUS_IMAGE_MATCH",
    "IMAGE_CASE_OR_PATH_MISMATCH",
    "INVALID_IMAGE_OVERRIDE",
    "UNKNOWN_LABEL",
    "NO_LABELSET_PROVIDED",
}


def is_system_metadata_path(path: str | Path) -> bool:
    parts = tuple(part for part in normalized(str(path)).split("/") if part)
    if not parts:
        return False
    folded = tuple(part.casefold() for part in parts)
    basename = parts[-1]
    return (
        "__macosx" in folded
        or "$recycle.bin" in folded
        or ".spotlight-v100" in folded
        or basename.startswith("._")
        or basename.startswith("~$")
        or basename.casefold() in SYSTEM_METADATA_NAMES
    )


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None


@dataclass
class ImageInfo:
    path: str
    basename: str
    width: int
    height: int
    extension: str
    format: str | None
    orientation: int | None
    file_size: int
    checksum_sha256: str
    progressive: bool = False


@dataclass
class FixSuggestion:
    id: str
    type: str
    title: str
    description: str
    confidence: str
    affected_count: int
    affected_cpc_files: list[str]
    before_example: str
    after_example: str
    accepted: bool = False


@dataclass
class CpcResult:
    cpc_path: str
    embedded_image_path: str | None = None
    embedded_image_name: str | None = None
    effective_image_path: str | None = None
    path_rules_applied: list[str] = field(default_factory=list)
    matched_image: str | None = None
    match_method: str | None = None
    match_confidence: str | None = None
    point_count: int = 0
    used_labels: list[str] = field(default_factory=list)
    scale_factor: int | None = None
    scale_diagnostics: dict[str, Any] = field(default_factory=dict)
    status: str = "error"
    action_category: str = "cannot_repair"
    issues: list[Issue] = field(default_factory=list)
    image_candidates: list[str] = field(default_factory=list)
    suggested_fix_id: str | None = None
    suggested_fix_accepted: bool = False
    optimization: dict[str, Any] | None = None


@dataclass
class ValidationReport:
    dataset_id: str
    summary: dict[str, int]
    dataset: dict[str, Any]
    cpc_results: list[CpcResult]
    global_issues: list[Issue]
    available_images: list[ImageInfo]
    label_codes: list[str]
    label_inventory: dict[str, dict[str, Any]]
    unknown_labels: dict[str, dict[str, Any]]
    suggested_fixes: list[FixSuggestion]
    optimization: dict[str, Any]
    pre_annotation: dict[str, Any]
    audit_preview: list[dict[str, Any]]
    package_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Workspace:
    root: Path
    original_files: Path
    prepared_package: Path
    optimized_files: Path
    preannotation_files: Path
    report: ValidationReport | None = None
    parsed_cpcs: dict[str, CpcFile] = field(default_factory=dict)
    file_lookup: dict[str, Path] = field(default_factory=dict)
    image_overrides: dict[str, str] = field(default_factory=dict)
    label_overrides: dict[str, str] = field(default_factory=dict)
    path_rules: list[dict[str, Any]] = field(default_factory=list)
    accepted_suggestions: set[str] = field(default_factory=set)
    label_mode: str = "id_only"
    optimize_jpegs: bool = False
    optimization_results: dict[str, JpegOptimizationResult] = field(default_factory=dict)
    optimized_lookup: dict[str, Path] = field(default_factory=dict)
    preannotation_enabled: bool = False
    preannotation_max_dimension: int = 4096
    preannotation_quality: int = 95
    preannotation_results: dict[str, PreAnnotationResult] = field(default_factory=dict)


WORKSPACES: dict[str, Workspace] = {}
WORKSPACE_MAX_AGE_SECONDS = int(os.environ.get("CPCE_POC_WORKSPACE_TTL", str(4 * 60 * 60)))
WORKSPACE_LIMIT = int(os.environ.get("CPCE_POC_WORKSPACE_LIMIT", "3"))
MIN_FREE_SPACE_BYTES = int(os.environ.get("CPCE_POC_MIN_FREE_BYTES", str(128 * 1024 * 1024)))


def _remove_workspace(dataset_id: str, workspace: Workspace) -> None:
    WORKSPACES.pop(dataset_id, None)
    shutil.rmtree(workspace.root, ignore_errors=True)


def cleanup_workspaces() -> None:
    now = time.time()
    for dataset_id, workspace in list(WORKSPACES.items()):
        try:
            age = now - workspace.root.stat().st_mtime
        except FileNotFoundError:
            WORKSPACES.pop(dataset_id, None)
            continue
        if age > WORKSPACE_MAX_AGE_SECONDS:
            _remove_workspace(dataset_id, workspace)

    remaining = sorted(
        WORKSPACES.items(),
        key=lambda item: item[1].root.stat().st_mtime if item[1].root.exists() else 0,
    )
    while len(remaining) >= WORKSPACE_LIMIT:
        dataset_id, workspace = remaining.pop(0)
        _remove_workspace(dataset_id, workspace)


def ensure_free_space(path: Path, additional_bytes: int = 0) -> None:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    required = max(MIN_FREE_SPACE_BYTES, additional_bytes + 32 * 1024 * 1024)
    if usage.free < required:
        free_mb = usage.free / (1024 * 1024)
        required_mb = required / (1024 * 1024)
        raise OSError(
            errno.ENOSPC,
            f"Not enough free disk space for this dataset. Available: {free_mb:.0f} MB; "
            f"required reserve: {required_mb:.0f} MB. Delete old cpce-poc temporary folders "
            "or free disk space, then retry.",
        )


def create_workspace() -> tuple[str, Workspace]:
    cleanup_workspaces()
    dataset_id = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix=f"cpce-poc-{dataset_id[:8]}-"))
    original = root / "original"
    original.mkdir(parents=True)
    workspace = Workspace(
        root=root,
        original_files=original,
        prepared_package=root / "coralnet-cpce-migration-package.zip",
        optimized_files=root / "optimized-jpegs",
        preannotation_files=root / "pre-annotation-images",
    )
    WORKSPACES[dataset_id] = workspace
    return dataset_id, workspace


def safe_relative_path(filename: str) -> Path:
    filename = filename.replace("\\", "/")
    parts = [part for part in filename.split("/") if part not in ("", ".", "..")]
    if not parts:
        raise ValueError("Invalid empty filename")
    return Path(*parts)


def expand_uploaded_archives(workspace: Workspace) -> None:
    for zip_path in list(workspace.original_files.rglob("*.zip")):
        extract_dir = workspace.original_files / zip_path.stem
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            members = [
                member
                for member in archive.infolist()
                if not member.is_dir() and not is_system_metadata_path(member.filename)
            ]
            total_uncompressed = sum(member.file_size for member in members)
            if total_uncompressed > 250 * 1024 * 1024 * 1024:
                raise ValueError("The ZIP expands beyond the 250 GB safety limit.")
            ensure_free_space(workspace.root, total_uncompressed)
            member_parts = [safe_relative_path(member.filename).parts for member in members]
            strip_redundant_root = bool(member_parts) and all(
                parts and parts[0].casefold() == zip_path.stem.casefold()
                for parts in member_parts
            )
            for member, parts in zip(members, member_parts):
                relative_parts = parts[1:] if strip_redundant_root else parts
                if not relative_parts:
                    continue
                target = extract_dir.joinpath(*relative_parts)
                if not str(target.resolve()).startswith(str(extract_dir.resolve())):
                    raise ValueError(f"Unsafe ZIP path: {member.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as dest:
                    shutil.copyfileobj(source, dest, length=1024 * 1024)
        zip_path.unlink(missing_ok=True)


def save_uploaded_files(uploaded: list[tuple[str, bytes]]) -> Workspace:
    _dataset_id, workspace = create_workspace()
    try:
        for filename, content in uploaded:
            ensure_free_space(workspace.root, len(content))
            destination = workspace.original_files / safe_relative_path(filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        expand_uploaded_archives(workspace)
        return workspace
    except Exception:
        shutil.rmtree(workspace.root, ignore_errors=True)
        for key, value in list(WORKSPACES.items()):
            if value is workspace:
                WORKSPACES.pop(key, None)
        raise


def normalized(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in normalized(path).split("/") if part)


def suffix_match_length(left: str, right: str, case_sensitive: bool = True) -> int:
    a = path_parts(left)
    b = path_parts(right)
    if not case_sensitive:
        a = tuple(item.casefold() for item in a)
        b = tuple(item.casefold() for item in b)
    count = 0
    for x, y in zip(reversed(a), reversed(b)):
        if x != y:
            break
        count += 1
    return count


def _path_has_prefix(path: str, prefix: str, case_sensitive: bool = False) -> bool:
    current = normalized(path).rstrip("/")
    expected = normalized(prefix).rstrip("/")
    if not expected:
        return False
    if not case_sensitive:
        current = current.casefold()
        expected = expected.casefold()
    return current == expected or current.startswith(expected + "/")


def apply_bulk_path_rules(
    embedded_path: str,
    cpc_path: str,
    rules: list[dict[str, Any]] | None,
) -> tuple[str, list[str]]:
    current = normalized(embedded_path)
    applied: list[str] = []
    for index, raw_rule in enumerate(rules or [], 1):
        if not isinstance(raw_rule, dict):
            continue
        rule_type = str(raw_rule.get("type", "")).strip().casefold()
        if rule_type == "prefix_replace":
            old_prefix = normalized(str(raw_rule.get("from", ""))).rstrip("/")
            new_prefix = normalized(str(raw_rule.get("to", ""))).rstrip("/")
            case_sensitive = bool(raw_rule.get("case_sensitive", False))
            if not old_prefix or not _path_has_prefix(current, old_prefix, case_sensitive):
                continue
            remainder = current[len(old_prefix):].lstrip("/")
            current = "/".join(part for part in (new_prefix, remainder) if part)
            applied.append(f"Rule {index}: replace {old_prefix} with {new_prefix or '[dataset root]'}")
        elif rule_type == "keep_last":
            try:
                count = int(raw_rule.get("count", 0))
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            parts = path_parts(current)
            if not parts:
                continue
            kept = "/".join(parts[-count:])
            prepend = normalized(str(raw_rule.get("prepend", ""))).rstrip("/")
            rewritten = "/".join(part for part in (prepend, kept) if part)
            if rewritten and rewritten != current:
                current = rewritten
                applied.append(
                    f"Rule {index}: keep last {count} path part(s)"
                    + (f" under {prepend}" if prepend else "")
                )
        elif rule_type == "cpc_folder":
            image_root = normalized(str(raw_rule.get("image_root", "images"))).rstrip("/")
            cpc_root = normalized(str(raw_rule.get("cpc_root", "cpc"))).rstrip("/")
            cpc_parts = list(path_parts(cpc_path))
            root_parts = list(path_parts(cpc_root))
            relative_parts = cpc_parts[:-1]
            if root_parts:
                folded = [part.casefold() for part in relative_parts]
                folded_root = [part.casefold() for part in root_parts]
                for start in range(0, len(relative_parts) - len(root_parts) + 1):
                    if folded[start:start + len(root_parts)] == folded_root:
                        relative_parts = relative_parts[start + len(root_parts):]
                        break
            filename = PureWindowsPath(embedded_path).name
            rewritten = "/".join([part for part in [image_root, *relative_parts, filename] if part])
            if rewritten and rewritten != current:
                current = rewritten
                applied.append(f"Rule {index}: use CPC folder under {image_root or '[dataset root]'}")
    return current, applied


def discover_files(workspace: Workspace) -> tuple[list[Path], list[Path], list[Path]]:
    all_files = [
        path
        for path in workspace.original_files.rglob("*")
        if path.is_file() and not is_system_metadata_path(path.relative_to(workspace.original_files))
    ]
    images = [path for path in all_files if path.suffix.lower() in IMAGE_EXTENSIONS]
    cpcs = [path for path in all_files if path.suffix.lower() == ".cpc"]
    csvs = [path for path in all_files if path.suffix.lower() == ".csv"]
    return images, cpcs, csvs


def relative_name(workspace: Workspace, path: Path) -> str:
    return normalized(str(path.relative_to(workspace.original_files)))


def load_images(workspace: Workspace, image_paths: list[Path]) -> tuple[list[ImageInfo], list[Issue]]:
    images: list[ImageInfo] = []
    issues: list[Issue] = []
    for image_path in image_paths:
        rel = relative_name(workspace, image_path)
        try:
            with Image.open(image_path) as image:
                width, height = image.size
                image_format = image.format
                progressive = bool(image.info.get("progressive") or image.info.get("progression"))
                try:
                    orientation = image.getexif().get(274)
                except (OSError, ValueError, TypeError):
                    orientation = None
            images.append(
                ImageInfo(
                    path=rel,
                    basename=image_path.name,
                    width=width,
                    height=height,
                    extension=image_path.suffix,
                    format=image_format,
                    orientation=int(orientation) if orientation is not None else None,
                    file_size=image_path.stat().st_size,
                    checksum_sha256=sha256_file(image_path),
                    progressive=progressive,
                )
            )
            workspace.file_lookup[rel] = image_path
        except (UnidentifiedImageError, OSError) as exc:
            issues.append(
                Issue(
                    severity="error",
                    code="UNREADABLE_IMAGE",
                    message=f"The image file {rel} could not be decoded.",
                    details={"error": str(exc)},
                    suggestion="Replace it with the original, non-corrupt image before importing.",
                )
            )
    return images, issues


def parse_label_codes_from_csv(csv_path: Path) -> set[str]:
    try:
        text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(StringIO(text))
        if not reader.fieldnames:
            return set()
        normalized_headers = {header.strip().casefold(): header for header in reader.fieldnames}
        selected = next(
            (normalized_headers[candidate] for candidate in LABEL_COLUMN_CANDIDATES if candidate in normalized_headers),
            None,
        )
        if selected is None:
            return set()
        return {
            (row.get(selected) or "").strip()
            for row in reader
            if (row.get(selected) or "").strip()
        }
    except OSError:
        return set()


def collect_label_codes(csv_paths: list[Path], manual_codes: str) -> set[str]:
    labels = {item.strip() for item in re.split(r"[,\n\r;]+", manual_codes or "") if item.strip()}
    for csv_path in csv_paths:
        labels.update(parse_label_codes_from_csv(csv_path))
    return labels


def _match_result(
    image: ImageInfo,
    method: str,
    confidence: str,
    issues: list[Issue],
) -> tuple[ImageInfo, str, str, list[str], list[Issue]]:
    return image, method, confidence, [image.path], issues


def find_image_match(
    cpc_path: str,
    embedded_path: str,
    images: list[ImageInfo],
    override: str | None,
) -> tuple[ImageInfo | None, str | None, str | None, list[str], list[Issue]]:
    issues: list[Issue] = []
    image_by_path = {image.path: image for image in images}
    if override:
        selected = image_by_path.get(override)
        if selected:
            return _match_result(selected, "manual override", "confirmed", issues)
        issues.append(
            Issue(
                severity="error",
                code="INVALID_IMAGE_OVERRIDE",
                message=f"The selected image mapping no longer exists: {override}",
            )
        )

    embedded_norm = normalized(embedded_path)
    exact = [image for image in images if image.path == embedded_norm]
    if len(exact) == 1:
        return _match_result(exact[0], "exact path", "very high", issues)

    exact_ci = [image for image in images if image.path.casefold() == embedded_norm.casefold()]
    if len(exact_ci) == 1:
        return _match_result(exact_ci[0], "case-insensitive exact path", "high", issues)

    scored = [(suffix_match_length(embedded_norm, image.path, True), image) for image in images]
    scored = [(score, image) for score, image in scored if score]
    if scored:
        best_score = max(score for score, _ in scored)
        best = [image for score, image in scored if score == best_score]
        if len(best) == 1:
            method = "unique filename" if best_score == 1 else f"unique path suffix ({best_score} parts)"
            confidence = "high" if best_score >= 1 else "medium"
            return _match_result(best[0], method, confidence, issues)
        candidates = sorted(image.path for image in best)
        issues.append(
            Issue(
                severity="error",
                code="AMBIGUOUS_IMAGE_MATCH",
                message="More than one uploaded image could belong to this CPC file.",
                details={"embedded_path": embedded_path, "candidates": candidates},
                suggestion="Choose the correct image or preserve more of the original survey folder path.",
            )
        )
        return None, None, None, candidates, issues

    scored_ci = [(suffix_match_length(embedded_norm, image.path, False), image) for image in images]
    scored_ci = [(score, image) for score, image in scored_ci if score]
    if scored_ci:
        best_score = max(score for score, _ in scored_ci)
        best = [image for score, image in scored_ci if score == best_score]
        if len(best) == 1:
            method = "case-insensitive unique filename" if best_score == 1 else "case-insensitive path suffix"
            return _match_result(best[0], method, "high", issues)
        candidates = sorted(image.path for image in best)
        issues.append(
            Issue(
                severity="error",
                code="AMBIGUOUS_IMAGE_MATCH",
                message="Filename capitalization differs and multiple possible images were found.",
                details={"candidates": candidates},
                suggestion="Choose the correct image manually.",
            )
        )
        return None, None, None, candidates, issues

    embedded_name = PureWindowsPath(embedded_path).name
    embedded_stem = Path(embedded_name).stem.casefold()
    extension_variants = [
        image for image in images
        if Path(image.basename).stem.casefold() == embedded_stem
        and image.extension.lower() in JPEG_EXTENSIONS
        and Path(embedded_name).suffix.lower() in JPEG_EXTENSIONS
    ]
    if len(extension_variants) == 1:
        return _match_result(extension_variants[0], "unique .jpg/.jpeg extension variant", "medium", issues)
    if len(extension_variants) > 1:
        candidates = sorted(image.path for image in extension_variants)
        issues.append(
            Issue(
                severity="error",
                code="AMBIGUOUS_IMAGE_MATCH",
                message="Multiple images share the expected filename stem.",
                details={"candidates": candidates},
            )
        )
        return None, None, None, candidates, issues

    cpc_stem = Path(cpc_path).stem.casefold()
    stem_matches = [image for image in images if Path(image.basename).stem.casefold() == cpc_stem]
    if len(stem_matches) == 1:
        return _match_result(stem_matches[0], "CPC filename stem", "medium", issues)

    fuzzy_names = difflib.get_close_matches(
        embedded_name.casefold(),
        [image.basename.casefold() for image in images],
        n=5,
        cutoff=0.55,
    )
    candidates = sorted(image.path for image in images if image.basename.casefold() in set(fuzzy_names))
    issues.append(
        Issue(
            severity="error",
            code="IMAGE_NOT_FOUND",
            message=f"No uploaded image matches the filename expected by {cpc_path}.",
            details={
                "expected_filename": embedded_name,
                "original_embedded_path": embedded_path,
                "searches_performed": [
                    "exact path",
                    "normalized path",
                    "path suffix",
                    "unique filename",
                    "case-insensitive filename",
                    ".jpg/.jpeg variant",
                    "CPC filename stem",
                ],
                "possible_similar_files": candidates,
            },
            suggestion=f"Locate the original {embedded_name} image and add it to the dataset.",
        )
    )
    return None, None, None, candidates, issues


def validate_scale_and_points(cpc: CpcFile, image: ImageInfo) -> tuple[int | None, dict[str, Any], list[Issue]]:
    issues: list[Issue] = []
    try:
        cpc_width = int(cpc.image_width)
        cpc_height = int(cpc.image_height)
    except ValueError:
        return None, {}, [
            Issue(
                severity="error",
                code="INVALID_CPC_DIMENSIONS",
                message="The CPC file does not contain valid integer image dimensions.",
                details={"cpc_width": cpc.image_width, "cpc_height": cpc.image_height},
                suggestion="Open the CPC file and verify line 1, or recover an unmodified copy.",
            )
        ]

    x_scale = cpc_width / image.width
    y_scale = cpc_height / image.height
    aspect_cpc = cpc_width / cpc_height if cpc_height else None
    aspect_image = image.width / image.height if image.height else None
    diagnostics = {
        "cpc_dimensions": [cpc_width, cpc_height],
        "uploaded_image_dimensions": [image.width, image.height],
        "width_scale": round(x_scale, 6),
        "height_scale": round(y_scale, 6),
        "cpc_aspect_ratio": round(aspect_cpc, 8) if aspect_cpc else None,
        "image_aspect_ratio": round(aspect_image, 8) if aspect_image else None,
        "plain_language": "",
    }

    if (
        x_scale <= 0
        or y_scale <= 0
        or not x_scale.is_integer()
        or not y_scale.is_integer()
        or x_scale != y_scale
    ):
        diagnostics["plain_language"] = (
            "The uploaded image does not preserve the exact pixel coordinate relationship expected by "
            "the CPC file. It may have been resized, cropped, rotated, re-exported, or replaced."
        )
        issues.append(
            Issue(
                severity="error",
                code="NON_INTEGER_SCALE_FACTOR",
                message="The uploaded image is not scientifically compatible with the CPC coordinates.",
                details=diagnostics,
                suggestion="Use the exact original image that was analyzed in CPCe. Do not resize or crop it.",
            )
        )
        return None, diagnostics, issues

    scale = int(x_scale)
    diagnostics["plain_language"] = (
        f"Compatible: both dimensions use the same integer scale factor ({scale})."
    )
    max_row = image.height - 1
    max_column = image.width - 1
    for point_number, point in enumerate(cpc.points, 1):
        try:
            x = int(point.x)
            if x < 0:
                raise ValueError
        except ValueError:
            issues.append(
                Issue(
                    severity="error",
                    code="INVALID_POINT_X",
                    message=f"Annotation point {point_number} has an invalid horizontal coordinate.",
                    details={"value": point.x},
                )
            )
            continue
        try:
            y = int(point.y)
            if y < 0:
                raise ValueError
        except ValueError:
            issues.append(
                Issue(
                    severity="error",
                    code="INVALID_POINT_Y",
                    message=f"Annotation point {point_number} has an invalid vertical coordinate.",
                    details={"value": point.y},
                )
            )
            continue
        column = int(round(x / scale))
        row = int(round(y / scale))
        if row > max_row or column > max_column:
            issues.append(
                Issue(
                    severity="error",
                    code="POINT_OUTSIDE_IMAGE",
                    message=f"Annotation point {point_number} falls outside the uploaded image.",
                    details={
                        "cpc_coordinates": [x, y],
                        "pixel_coordinates": [column, row],
                        "valid_pixel_bounds": {"x": [0, max_column], "y": [0, max_row]},
                    },
                    suggestion="Verify that this is the original image and CPC file. Coordinates are not altered automatically.",
                )
            )
    return scale, diagnostics, issues


def source_label_for_point(cpc_point: Any, label_mode: str) -> str:
    if label_mode == "id_and_notes" and cpc_point.label_id and cpc_point.notes:
        return f"{cpc_point.label_id}+{cpc_point.notes}"
    return cpc_point.label_id


def label_suggestions(code: str, label_codes: set[str]) -> list[str]:
    return difflib.get_close_matches(code, sorted(label_codes), n=5, cutoff=0.35)


def _fix_kind(match_method: str) -> tuple[str, str, str]:
    method = match_method.casefold()
    if "case-insensitive" in method:
        return (
            "normalize_case",
            "Fix filename capitalization",
            "The image was found uniquely after ignoring capitalization differences.",
        )
    if "extension variant" in method:
        return (
            "normalize_jpeg_extension",
            "Fix .jpg/.jpeg reference",
            "The image was found uniquely after treating .jpg and .jpeg as equivalent JPEG extensions.",
        )
    if "filename" in method or "stem" in method:
        return (
            "discard_obsolete_folders",
            "Match images by unique filename",
            "The old CPCe folder path no longer exists, but each expected filename maps to one uploaded image.",
        )
    return (
        "rewrite_embedded_path",
        "Rewrite obsolete embedded image paths",
        "The uploaded folder structure uniquely identifies the correct image.",
    )


def _stable_suggestion_id(kind: str, cpc_files: list[str], targets: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(kind.encode())
    for value in sorted(cpc_files) + sorted(targets):
        digest.update(b"\0")
        digest.update(value.encode("utf-8", errors="replace"))
    return f"{kind}-{digest.hexdigest()[:12]}"


def _classify_result(result: CpcResult) -> None:
    codes = {issue.code for issue in result.issues}
    if codes & CANNOT_SAFELY_REPAIR_CODES:
        result.action_category = "cannot_repair"
        result.status = "error"
    elif codes & NEEDS_REVIEW_CODES:
        result.action_category = "needs_review"
        result.status = "error" if any(issue.severity == "error" for issue in result.issues) else "warning"
    elif result.suggested_fix_id and not result.suggested_fix_accepted:
        result.action_category = "auto_fix"
        result.status = "warning"
    elif any(issue.severity == "error" for issue in result.issues):
        result.action_category = "needs_review"
        result.status = "error"
    else:
        result.action_category = "ready"
        result.status = "warning" if any(issue.severity == "warning" for issue in result.issues) else "ready"


def _run_jpeg_optimization(workspace: Workspace, results: list[CpcResult], global_issues: list[Issue]) -> dict[str, Any]:
    workspace.optimization_results = {}
    workspace.optimized_lookup = {}
    shutil.rmtree(workspace.optimized_files, ignore_errors=True)
    workspace.optimized_files.mkdir(parents=True, exist_ok=True)
    available = resolve_jpegtran()
    summary: dict[str, Any] = {
        "enabled": workspace.optimize_jpegs,
        "jpegtran_available": bool(available),
        "jpegtran_path": available,
        "eligible": 0,
        "optimized": 0,
        "kept_original_no_savings": 0,
        "rejected": 0,
        "skipped": 0,
        "bytes_saved": 0,
        "percent_reduction": 0.0,
        "warning": (
            "Lossless JPEG optimization can reduce transfer size, but it does not reduce pixel count "
            "or decoded memory requirements inside CoralNet."
        ),
    }
    if not workspace.optimize_jpegs:
        return summary
    if not available:
        global_issues.append(
            Issue(
                severity="warning",
                code="JPEGTRAN_NOT_AVAILABLE",
                message="CPCe-safe lossless JPEG optimization was requested, but jpegtran was not found.",
                suggestion="Install libjpeg-turbo or set JPEGTRAN_BIN. Original images will be preserved.",
            )
        )
        return summary

    by_image: defaultdict[str, list[CpcResult]] = defaultdict(list)
    for result in results:
        if result.matched_image:
            by_image[result.matched_image].append(result)

    for image_rel, related in by_image.items():
        source_path = workspace.file_lookup.get(image_rel)
        if source_path is None:
            continue
        if any(item.action_category == "cannot_repair" for item in related) or len(related) != 1:
            optimization = JpegOptimizationResult(
                image_path=image_rel,
                status="skipped",
                eligible=False,
                message="Optimization was skipped because the CPC/image relationship has a blocking issue.",
            )
        else:
            cpc = workspace.parsed_cpcs[related[0].cpc_path]
            optimization = optimize_jpeg_losslessly(
                source_path=source_path,
                relative_path=image_rel,
                output_root=workspace.optimized_files,
                expected_cpc_filename=cpc.embedded_image_name,
                jpegtran_bin=available,
            )
        workspace.optimization_results[image_rel] = optimization
        if optimization.status == "optimized" and optimization.output_path:
            workspace.optimized_lookup[image_rel] = Path(optimization.output_path)
        for item in related:
            item.optimization = optimization.to_dict()
            if optimization.status in {"rejected", "unavailable"}:
                item.issues.append(
                    Issue(
                        severity="warning",
                        code="JPEG_OPTIMIZATION_REJECTED",
                        message=optimization.message,
                        details={"errors": optimization.errors, "warnings": optimization.warnings},
                        suggestion="The original image will be included unchanged.",
                    )
                )
        if optimization.eligible:
            summary["eligible"] += 1
        summary[optimization.status] = summary.get(optimization.status, 0) + 1
        summary["bytes_saved"] += optimization.bytes_saved

    original_bytes = sum(
        value.original.file_size
        for value in workspace.optimization_results.values()
        if value.original is not None
    )
    summary["percent_reduction"] = (
        round((summary["bytes_saved"] / original_bytes) * 100, 3) if original_bytes else 0.0
    )
    global_issues.append(
        Issue(
            severity="warning",
            code="JPEG_OPTIMIZATION_MEMORY_WARNING",
            message=(
                "CPCe-safe lossless JPEG optimization may reduce upload size, but it does not reduce "
                "the image pixel count or decoded memory requirements."
            ),
            suggestion="Very high-resolution images may still create CoralNet processing or performance issues.",
        )
    )
    return summary


def _run_preannotation_workflow(
    workspace: Workspace,
    images: list[ImageInfo],
    results: list[CpcResult],
    global_issues: list[Issue],
) -> dict[str, Any]:
    workspace.preannotation_results = {}
    shutil.rmtree(workspace.preannotation_files, ignore_errors=True)
    summary: dict[str, Any] = {
        "enabled": workspace.preannotation_enabled,
        "eligible": 0,
        "prepared": 0,
        "skipped": 0,
        "errors": 0,
        "max_long_edge": workspace.preannotation_max_dimension,
        "quality": workspace.preannotation_quality,
        "warning": (
            "This separate workflow changes pixels and dimensions and must never be used for an image "
            "that already has CPCe annotation coordinates."
        ),
        "results": [],
    }
    if not workspace.preannotation_enabled:
        return summary

    matched = {result.matched_image for result in results if result.matched_image}
    referenced_basenames = {
        cpc.embedded_image_name.casefold() for cpc in workspace.parsed_cpcs.values()
    }
    for image in images:
        if image.path in matched or image.basename.casefold() in referenced_basenames:
            continue
        summary["eligible"] += 1
        source = workspace.file_lookup[image.path]
        prepared = prepare_unannotated_jpeg(
            source_path=source,
            relative_path=image.path,
            output_root=workspace.preannotation_files,
            max_long_edge=workspace.preannotation_max_dimension,
            quality=workspace.preannotation_quality,
        )
        workspace.preannotation_results[image.path] = prepared
        summary[prepared.status if prepared.status in {"prepared", "skipped"} else "errors"] += 1
        summary["results"].append(prepared.to_dict())

    global_issues.append(
        Issue(
            severity="warning",
            code="PREANNOTATION_WORKFLOW_ENABLED",
            message=(
                "The optional pre-annotation workflow is enabled only for images that have no CPC file "
                "and do not share a filename with any CPC image reference."
            ),
            suggestion="Use these outputs as the canonical images before creating new CPCe annotations.",
        )
    )
    return summary


def validate_workspace(
    workspace: Workspace,
    manual_label_codes: str = "",
    label_mode: str = "id_only",
    image_overrides: dict[str, str] | None = None,
    label_overrides: dict[str, str] | None = None,
    path_rules: list[dict[str, Any]] | None = None,
    accepted_suggestions: list[str] | set[str] | None = None,
    optimize_jpegs: bool = False,
    preannotation_enabled: bool = False,
    preannotation_max_dimension: int = 4096,
    preannotation_quality: int = 95,
) -> ValidationReport:
    image_overrides = image_overrides or {}
    label_overrides = label_overrides or {}
    path_rules = path_rules or []
    workspace.image_overrides = image_overrides
    workspace.label_overrides = label_overrides
    workspace.path_rules = path_rules
    workspace.accepted_suggestions = set(accepted_suggestions or [])
    workspace.label_mode = label_mode
    workspace.optimize_jpegs = optimize_jpegs
    workspace.preannotation_enabled = preannotation_enabled
    workspace.preannotation_max_dimension = max(256, int(preannotation_max_dimension))
    workspace.preannotation_quality = max(1, min(100, int(preannotation_quality)))
    workspace.parsed_cpcs = {}
    workspace.file_lookup = {}

    image_paths, cpc_paths, csv_paths = discover_files(workspace)
    images, global_issues = load_images(workspace, image_paths)
    label_codes = collect_label_codes(csv_paths, manual_label_codes)
    label_codes_casefold = {code.casefold(): code for code in label_codes}

    basename_counts = Counter(image.basename.casefold() for image in images)
    duplicates = {
        basename: sorted(image.path for image in images if image.basename.casefold() == basename)
        for basename, count in basename_counts.items()
        if count > 1
    }
    for _basename, paths in duplicates.items():
        global_issues.append(
            Issue(
                severity="warning",
                code="DUPLICATE_IMAGE_FILENAME",
                message=f"The filename {Path(paths[0]).name} appears in more than one folder.",
                details={"paths": paths},
                suggestion="The tool will use path context when possible and ask for confirmation when ambiguous.",
            )
        )

    results: list[CpcResult] = []
    label_point_counter: Counter[str] = Counter()
    label_file_counter: defaultdict[str, set[str]] = defaultdict(set)
    cpcs_per_image: defaultdict[str, list[str]] = defaultdict(list)
    auto_candidates: defaultdict[str, list[CpcResult]] = defaultdict(list)

    for cpc_path in sorted(cpc_paths):
        rel = relative_name(workspace, cpc_path)
        workspace.file_lookup[rel] = cpc_path
        result = CpcResult(cpc_path=rel)
        try:
            cpc = CpcFile.parse(cpc_path.read_text(encoding="utf-8-sig", errors="replace"))
            workspace.parsed_cpcs[rel] = cpc
        except (OSError, CpcParseError) as exc:
            result.issues.append(
                Issue(
                    severity="error",
                    code="CPC_PARSE_ERROR",
                    message=f"The CPC file could not be parsed: {exc}",
                    suggestion="Recover an unmodified CPC file or repair its line structure before importing.",
                )
            )
            _classify_result(result)
            results.append(result)
            continue

        result.embedded_image_path = cpc.image_filepath
        result.embedded_image_name = cpc.embedded_image_name
        result.point_count = len(cpc.points)
        effective_path, applied_rules = apply_bulk_path_rules(cpc.image_filepath, rel, path_rules)
        result.effective_image_path = effective_path
        result.path_rules_applied = applied_rules

        image, method, confidence, candidates, match_issues = find_image_match(
            rel, effective_path, images, image_overrides.get(rel)
        )
        result.image_candidates = candidates
        result.issues.extend(match_issues)
        if image:
            result.matched_image = image.path
            result.match_method = f"bulk path rule → {method}" if applied_rules and method else method
            result.match_confidence = confidence
            cpcs_per_image[image.path].append(rel)
            scale, diagnostics, scale_issues = validate_scale_and_points(cpc, image)
            result.scale_factor = scale
            result.scale_diagnostics = diagnostics
            result.issues.extend(scale_issues)
            if (
                method not in {"exact path", "manual override"}
                and not applied_rules
                and normalized(cpc.image_filepath) != image.path
            ):
                kind, _title, _description = _fix_kind(method or "")
                auto_candidates[kind].append(result)

        used_labels: list[str] = []
        for point in cpc.points:
            source_code = source_label_for_point(point, label_mode)
            if not source_code:
                continue
            used_labels.append(source_code)
            label_point_counter[source_code] += 1
            label_file_counter[source_code].add(rel)
        result.used_labels = sorted(set(used_labels), key=str.casefold)

        if not label_codes:
            result.issues.append(
                Issue(
                    severity="warning",
                    code="NO_LABELSET_PROVIDED",
                    message="Label validation has not been completed for the destination CoralNet Source.",
                    suggestion="Upload a CoralNet labelset CSV or paste the Source short codes.",
                )
            )
        else:
            for source_code in result.used_labels:
                target_code = label_overrides.get(source_code, source_code).strip()
                if target_code.casefold() not in label_codes_casefold:
                    result.issues.append(
                        Issue(
                            severity="error",
                            code="UNKNOWN_LABEL",
                            message=f"CPCe code {source_code} does not exist in the destination labelset.",
                            details={
                                "cpc_code": source_code,
                                "mapped_code": target_code,
                                "suggestions": label_suggestions(target_code, label_codes),
                            },
                            suggestion="Map this code once in the dataset-level label table.",
                        )
                    )
        results.append(result)

    suggested_fixes: list[FixSuggestion] = []
    audit_preview: list[dict[str, Any]] = []
    for kind, affected in sorted(auto_candidates.items()):
        cpc_files = [item.cpc_path for item in affected]
        targets = [item.matched_image or "" for item in affected]
        suggestion_id = _stable_suggestion_id(kind, cpc_files, targets)
        _kind, title, description = _fix_kind(affected[0].match_method or "")
        accepted = suggestion_id in workspace.accepted_suggestions
        confidence_rank = {"very high": 4, "high": 3, "medium": 2, "low": 1, "confirmed": 5}
        group_confidence = min(
            (item.match_confidence or "medium" for item in affected),
            key=lambda value: confidence_rank.get(value, 0),
            default="medium",
        )
        suggestion = FixSuggestion(
            id=suggestion_id,
            type=kind,
            title=title,
            description=description,
            confidence=group_confidence,
            affected_count=len(affected),
            affected_cpc_files=sorted(cpc_files),
            before_example=affected[0].embedded_image_path or "",
            after_example=affected[0].matched_image or "",
            accepted=accepted,
        )
        suggested_fixes.append(suggestion)
        for item in affected:
            item.suggested_fix_id = suggestion_id
            item.suggested_fix_accepted = accepted
            if accepted:
                audit_preview.append(
                    {
                        "change_type": "image_path",
                        "cpc_file": item.cpc_path,
                        "original_value": item.embedded_image_path,
                        "new_value": item.matched_image,
                        "reason": title,
                        "approval": "user-approved suggested fix",
                    }
                )
            else:
                item.issues.append(
                    Issue(
                        severity="warning",
                        code="SAFE_PATH_REPAIR_AVAILABLE",
                        message=f"A safe bulk path repair is available: {title}.",
                        details={
                            "suggestion_id": suggestion_id,
                            "before": item.embedded_image_path,
                            "after": item.matched_image,
                            "confidence": item.match_confidence,
                        },
                        suggestion="Approve the suggested fix to include this CPC file in READY output.",
                    )
                )

    for image_path, related_cpcs in cpcs_per_image.items():
        if len(related_cpcs) <= 1:
            continue
        for result in results:
            if result.matched_image == image_path:
                result.issues.append(
                    Issue(
                        severity="error",
                        code="MULTIPLE_CPCS_FOR_IMAGE",
                        message=f"More than one CPC file is mapped to {image_path}.",
                        details={"cpc_files": sorted(related_cpcs)},
                        suggestion="Review the duplicates and keep exactly one correct CPC file per image.",
                    )
                )

    label_inventory: dict[str, dict[str, Any]] = {}
    for code, count in sorted(label_point_counter.items(), key=lambda item: item[0].casefold()):
        target = label_overrides.get(code, code).strip()
        if not label_codes:
            status = "not_checked"
        elif target.casefold() in label_codes_casefold:
            status = "mapped" if target != code else "exact_match"
        else:
            status = "unknown"
        label_inventory[code] = {
            "point_count": count,
            "file_count": len(label_file_counter[code]),
            "mapped_to": label_overrides.get(code, ""),
            "effective_code": target,
            "status": status,
            "suggestions": label_suggestions(target, label_codes),
        }
        if status == "mapped":
            audit_preview.append(
                {
                    "change_type": "label_mapping",
                    "cpc_file": f"{len(label_file_counter[code])} file(s)",
                    "original_value": code,
                    "new_value": target,
                    "reason": f"Dataset-level mapping for {count} annotation point(s)",
                    "approval": "user-confirmed label mapping",
                }
            )

    unknown_labels = {
        code: info for code, info in label_inventory.items() if info["status"] == "unknown"
    }

    for result in results:
        _classify_result(result)

    optimization_summary = _run_jpeg_optimization(workspace, results, global_issues)
    for result in results:
        _classify_result(result)

    matched_images = {result.matched_image for result in results if result.matched_image}
    orphan_images = sorted(image.path for image in images if image.path not in matched_images)
    if orphan_images:
        global_issues.append(
            Issue(
                severity="warning",
                code="IMAGES_WITHOUT_CPC",
                message=f"{len(orphan_images)} image(s) are not matched to a CPC file.",
                details={"images": orphan_images[:100]},
                suggestion="Remove unrelated images, add missing CPC files, or use the separate pre-annotation workflow.",
            )
        )

    preannotation_summary = _run_preannotation_workflow(workspace, images, results, global_issues)

    if not cpc_paths:
        global_issues.append(Issue(severity="error", code="NO_CPC_FILES", message="No .cpc files were found."))
    if not images:
        global_issues.append(Issue(severity="error", code="NO_IMAGES", message="No supported image files were found."))

    action_counts = Counter(result.action_category for result in results)
    all_issues = global_issues + [issue for result in results for issue in result.issues]
    pending_fixes = sum(not fix.accepted for fix in suggested_fixes)
    summary = {
        "cpc_files": len(cpc_paths),
        "images": len(images),
        "ready": action_counts["ready"],
        "automatic_repairable": action_counts["auto_fix"],
        "needs_review": action_counts["needs_review"],
        "cannot_safely_repair": action_counts["cannot_repair"],
        "warnings": sum(result.status == "warning" for result in results),
        "errors": sum(result.status == "error" for result in results),
        "issue_count": len(all_issues),
        "pending_suggested_fixes": pending_fixes,
    }
    blocking_global = any(issue.severity == "error" for issue in global_issues)
    package_ready = (
        not blocking_global
        and action_counts["needs_review"] == 0
        and action_counts["cannot_repair"] == 0
        and pending_fixes == 0
    )
    report = ValidationReport(
        dataset_id=workspace.root.name,
        summary=summary,
        dataset={
            "duplicate_basenames": duplicates,
            "orphan_images": orphan_images,
            "label_mode": label_mode,
            "path_rules": path_rules,
            "path_rules_applied_to_cpcs": sum(bool(result.path_rules_applied) for result in results),
            "matched_after_path_rewrite": sum(
                bool(result.path_rules_applied and result.matched_image) for result in results
            ),
            "ignored_metadata_patterns": ["__MACOSX", "._*", ".DS_Store", "Thumbs.db", "desktop.ini"],
        },
        cpc_results=results,
        global_issues=global_issues,
        available_images=sorted(images, key=lambda item: item.path.casefold()),
        label_codes=sorted(label_codes, key=str.casefold),
        label_inventory=label_inventory,
        unknown_labels=unknown_labels,
        suggested_fixes=suggested_fixes,
        optimization=optimization_summary,
        pre_annotation=preannotation_summary,
        audit_preview=audit_preview,
        package_ready=package_ready,
    )
    workspace.report = report
    return report


def _write_csv(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
    stream = StringIO()
    writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def _html_report(report: ValidationReport) -> str:
    category_labels = {
        "ready": "Ready",
        "auto_fix": "Automatic repair available",
        "needs_review": "Needs review",
        "cannot_repair": "Cannot safely repair",
    }
    rows = []
    for result in report.cpc_results:
        issue_text = "; ".join(issue.message for issue in result.issues) or "No issues"
        rows.append(
            "<tr>"
            f"<td>{html.escape(category_labels.get(result.action_category, result.action_category))}</td>"
            f"<td>{html.escape(result.cpc_path)}</td>"
            f"<td>{html.escape(result.matched_image or '—')}</td>"
            f"<td>{html.escape(issue_text)}</td>"
            "</tr>"
        )
    fixes = "".join(
        f"<li><strong>{html.escape(fix.title)}</strong>: {fix.affected_count} file(s); "
        f"{'approved' if fix.accepted else 'pending approval'}.</li>"
        for fix in report.suggested_fixes
    ) or "<li>No automatic path repairs were needed.</li>"
    labels = "".join(
        f"<tr><td>{html.escape(code)}</td><td>{info['point_count']}</td>"
        f"<td>{html.escape(info['effective_code'])}</td><td>{html.escape(info['status'])}</td></tr>"
        for code, info in report.label_inventory.items()
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>CoralNet CPCe Validation Report</title>
<style>body{{font-family:Arial,sans-serif;max-width:1100px;margin:40px auto;color:#17343b}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd9dc;padding:8px;text-align:left;vertical-align:top}}th{{background:#eef5f5}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{border:1px solid #ccd9dc;border-radius:8px;padding:12px;min-width:140px}}code{{white-space:pre-wrap}}</style></head>
<body><h1>CoralNet Legacy CPCe Migration Validation Report</h1>
<p>Dataset ID: {html.escape(report.dataset_id)}</p>
<div class="cards">
<div class="card"><strong>{report.summary['ready']}</strong><br>Ready</div>
<div class="card"><strong>{report.summary['automatic_repairable']}</strong><br>Automatic repair</div>
<div class="card"><strong>{report.summary['needs_review']}</strong><br>Needs review</div>
<div class="card"><strong>{report.summary['cannot_safely_repair']}</strong><br>Cannot safely repair</div>
</div>
<h2>Suggested path repairs</h2><ul>{fixes}</ul>
<h2>Label inventory</h2><table><tr><th>CPCe code</th><th>Points</th><th>Effective CoralNet code</th><th>Status</th></tr>{labels}</table>
<h2>JPEG optimization</h2><p>{html.escape(report.optimization.get('warning',''))}</p><pre>{html.escape(json.dumps(report.optimization, indent=2))}</pre>
<h2>File results</h2><table><tr><th>Category</th><th>CPC file</th><th>Matched image</th><th>Diagnostic</th></tr>{''.join(rows)}</table>
</body></html>"""


def build_prepared_package(workspace: Workspace) -> Path:
    if workspace.report is None:
        raise ValueError("Validate the dataset before building a package.")
    report = workspace.report
    result_by_cpc = {result.cpc_path: result for result in report.cpc_results}
    estimated_bytes = sum(
        workspace.file_lookup[result.matched_image].stat().st_size
        for result in report.cpc_results
        if result.matched_image and result.matched_image in workspace.file_lookup
        and result.action_category == "ready"
    )
    ensure_free_space(workspace.root, estimated_bytes // 2)
    workspace.prepared_package.unlink(missing_ok=True)

    audit_rows: list[dict[str, Any]] = list(report.audit_preview)
    label_mapping_rows: list[dict[str, Any]] = []
    optimization_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    copied_ready_images: set[str] = set()

    try:
        with zipfile.ZipFile(
            workspace.prepared_package,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for cpc_rel, parsed in workspace.parsed_cpcs.items():
                result = result_by_cpc[cpc_rel]
                source_cpc = workspace.file_lookup[cpc_rel]
                summary_rows.append(
                    {
                        "cpc_file": cpc_rel,
                        "category": result.action_category,
                        "embedded_image": result.embedded_image_path or "",
                        "matched_image": result.matched_image or "",
                        "point_count": result.point_count,
                        "scale_factor": result.scale_factor or "",
                        "issue_codes": "; ".join(issue.code for issue in result.issues),
                    }
                )
                if result.action_category != "ready" or not result.matched_image:
                    group = "EXCLUDED" if result.action_category == "cannot_repair" else "NEEDS_ATTENTION"
                    archive.write(source_cpc, f"{group}/cpc/{normalized(cpc_rel)}")
                    if any(issue.code == "IMAGE_NOT_FOUND" for issue in result.issues):
                        missing_rows.append(
                            {
                                "cpc_file": cpc_rel,
                                "expected_filename": result.embedded_image_name or "",
                                "embedded_path": result.embedded_image_path or "",
                                "suggested_action": "Locate and add the exact original image.",
                            }
                        )
                    continue

                image_rel = result.matched_image
                source_image = workspace.file_lookup[image_rel]
                prepared_image = workspace.optimized_lookup.get(image_rel, source_image)
                if image_rel not in copied_ready_images:
                    archive.write(prepared_image, f"READY/images/{normalized(image_rel)}")
                    copied_ready_images.add(image_rel)

                cpc = copy.deepcopy(parsed)
                original_path = cpc.image_filepath
                cpc.image_filepath = str(PureWindowsPath(image_rel))
                if original_path != cpc.image_filepath:
                    audit_rows.append(
                        {
                            "change_type": "image_path",
                            "cpc_file": cpc_rel,
                            "original_value": original_path,
                            "new_value": cpc.image_filepath,
                            "reason": result.match_method or "validated image match",
                            "approval": "validated/approved",
                        }
                    )
                for point in cpc.points:
                    source_code = source_label_for_point(point, workspace.label_mode)
                    target_code = workspace.label_overrides.get(source_code, "").strip()
                    if target_code:
                        point.label_id = target_code
                        if workspace.label_mode == "id_and_notes":
                            point.notes = ""
                        label_mapping_rows.append(
                            {
                                "cpc_file": cpc_rel,
                                "original_code": source_code,
                                "prepared_code": target_code,
                            }
                        )
                archive.writestr(f"READY/cpc/{normalized(cpc_rel)}", cpc.to_text().encode("utf-8"))

            for image_rel, optimization in workspace.optimization_results.items():
                original = optimization.original
                optimized = optimization.optimized
                optimization_rows.append(
                    {
                        "image": image_rel,
                        "status": optimization.status,
                        "original_size": original.file_size if original else "",
                        "optimized_size": optimized.file_size if optimized else "",
                        "bytes_saved": optimization.bytes_saved,
                        "percent_reduction": optimization.percent_reduction,
                        "width": original.width if original else "",
                        "height": original.height if original else "",
                        "orientation": original.exif_orientation if original else "",
                        "original_checksum": original.checksum_sha256 if original else "",
                        "optimized_checksum": optimized.checksum_sha256 if optimized else "",
                        "errors": "; ".join(optimization.errors),
                        "warnings": "; ".join(optimization.warnings),
                    }
                )
                if optimization.status == "optimized":
                    audit_rows.append(
                        {
                            "change_type": "lossless_jpeg_optimization",
                            "cpc_file": image_rel,
                            "original_value": f"{optimization.original.file_size} bytes",
                            "new_value": f"{optimization.optimized.file_size} bytes",
                            "reason": "jpegtran -copy all -optimize; decoded pixels verified identical",
                            "approval": "user enabled CPCe-safe optimization",
                        }
                    )

            for image_rel, prepared in workspace.preannotation_results.items():
                if prepared.status == "prepared" and prepared.output_path:
                    archive.write(
                        Path(prepared.output_path),
                        f"PRE_ANNOTATION_ONLY/images/{normalized(image_rel)}",
                    )
                    audit_rows.append(
                        {
                            "change_type": "pre_annotation_image_preparation",
                            "cpc_file": image_rel,
                            "original_value": f"{prepared.original_width}x{prepared.original_height}; orientation {prepared.original_orientation}",
                            "new_value": f"{prepared.output_width}x{prepared.output_height}; orientation 1",
                            "reason": "Explicit optional workflow for images with no CPC annotations",
                            "approval": "user enabled pre-annotation workflow",
                        }
                    )

            archive.writestr(
                "reports/validation-report.json",
                json.dumps(report.to_dict(), indent=2).encode("utf-8"),
            )
            archive.writestr("reports/validation-report.html", _html_report(report).encode("utf-8"))
            archive.writestr(
                "reports/validation-summary.csv",
                _write_csv(
                    summary_rows,
                    ["cpc_file", "category", "embedded_image", "matched_image", "point_count", "scale_factor", "issue_codes"],
                ),
            )
            archive.writestr(
                "reports/change-audit.csv",
                _write_csv(
                    audit_rows,
                    ["change_type", "cpc_file", "original_value", "new_value", "reason", "approval"],
                ),
            )
            archive.writestr(
                "reports/missing-images.csv",
                _write_csv(
                    missing_rows,
                    ["cpc_file", "expected_filename", "embedded_path", "suggested_action"],
                ),
            )
            label_rows = [
                {"cpc_code": code, **info} for code, info in report.label_inventory.items()
            ]
            archive.writestr(
                "reports/label-inventory.csv",
                _write_csv(
                    label_rows,
                    ["cpc_code", "point_count", "file_count", "mapped_to", "effective_code", "status", "suggestions"],
                ),
            )
            archive.writestr(
                "reports/label-mapping.csv",
                _write_csv(label_mapping_rows, ["cpc_file", "original_code", "prepared_code"]),
            )
            archive.writestr(
                "reports/jpeg-optimization.csv",
                _write_csv(
                    optimization_rows,
                    [
                        "image", "status", "original_size", "optimized_size", "bytes_saved",
                        "percent_reduction", "width", "height", "orientation",
                        "original_checksum", "optimized_checksum", "errors", "warnings",
                    ],
                ),
            )
            readme = (
                "CoralNet Legacy CPCe Migration Package\n"
                "======================================\n\n"
                "1. Review reports/validation-report.html and reports/change-audit.csv.\n"
                "2. Upload files under READY/images to the destination CoralNet Source.\n"
                "3. Confirm that every effective code in reports/label-inventory.csv exists in the Source.\n"
                "4. Upload files under READY/cpc through CoralNet's CPCe annotation upload workflow.\n"
                "5. Do not upload NEEDS_ATTENTION or EXCLUDED files until their issues are resolved.\n\n"
                "CPCe-safe lossless JPEG optimization never resizes, crops, rotates, flips, auto-orients, "
                "renames, or performs a lossy JPEG re-export. It may reduce file transfer size, but it "
                "does not reduce image pixel count or decoded memory requirements.\n\n"
                "PRE_ANNOTATION_ONLY is a separate, explicitly enabled workflow. Those images have no CPC "
                "annotations and may have been auto-oriented or resized. They must become the canonical "
                "images before new CPCe annotation points are created.\n"
            )
            archive.writestr("README-CORALNET-IMPORT.txt", readme.encode("utf-8"))
    except Exception:
        workspace.prepared_package.unlink(missing_ok=True)
        raise
    return workspace.prepared_package
