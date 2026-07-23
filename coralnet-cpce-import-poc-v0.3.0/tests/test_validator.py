from pathlib import Path
import zipfile

from PIL import Image

from validator import build_prepared_package, discover_files, save_uploaded_files, validate_workspace


def cpc_text(image_path: str, width: int = 12000, height: int = 9000, label: str = "ACR") -> str:
    lines = [
        f'"codes.txt","{image_path}",{width},{height},800,600',
        "0,0", "0,9000", "12000,9000", "12000,0",
        "1", "1500,1500", f'"1","{label}","Notes",""',
    ]
    return "\r\n".join(lines) + "\r\n"


def image_bytes(tmp_path: Path, size=(800, 600), name="image.jpg") -> bytes:
    path = tmp_path / name
    Image.new("RGB", size, (30, 80, 120)).save(path, "JPEG")
    return path.read_bytes()


def accept_all(workspace, report, **kwargs):
    return validate_workspace(
        workspace,
        accepted_suggestions=[fix.id for fix in report.suggested_fixes],
        **kwargs,
    )


def test_automatic_unique_filename_fix_requires_approval(tmp_path):
    workspace = save_uploaded_files([
        ("images/reef.jpg", image_bytes(tmp_path)),
        ("cpc/reef.cpc", cpc_text(r"D:\Survey\reef.jpg").encode()),
    ])
    report = validate_workspace(workspace, manual_label_codes="ACR")
    assert report.summary["automatic_repairable"] == 1
    assert report.suggested_fixes[0].type == "discard_obsolete_folders"
    assert report.cpc_results[0].matched_image == "images/reef.jpg"

    approved = accept_all(workspace, report, manual_label_codes="ACR")
    assert approved.summary["ready"] == 1
    assert approved.package_ready is True
    assert approved.audit_preview[0]["change_type"] == "image_path"


def test_exact_relative_path_is_ready_without_suggestion(tmp_path):
    workspace = save_uploaded_files([
        ("images/reef.jpg", image_bytes(tmp_path)),
        ("cpc/reef.cpc", cpc_text("images/reef.jpg").encode()),
    ])
    report = validate_workspace(workspace, manual_label_codes="ACR")
    assert report.summary["ready"] == 1
    assert report.suggested_fixes == []


def test_scale_error_is_cannot_safely_repair(tmp_path):
    workspace = save_uploaded_files([
        ("images/reef.jpg", image_bytes(tmp_path, (640, 480))),
        ("cpc/reef.cpc", cpc_text(r"D:\Survey\reef.jpg").encode()),
    ])
    report = validate_workspace(workspace, manual_label_codes="ACR")
    result = report.cpc_results[0]
    assert result.action_category == "cannot_repair"
    assert "NON_INTEGER_SCALE_FACTOR" in [issue.code for issue in result.issues]
    assert "resized" in result.scale_diagnostics["plain_language"]


def test_dataset_level_label_mapping(tmp_path):
    workspace = save_uploaded_files([
        ("images/a.jpg", image_bytes(tmp_path, name="a.jpg")),
        ("images/b.jpg", image_bytes(tmp_path, name="b.jpg")),
        ("cpc/a.cpc", cpc_text("images/a.jpg", label="ALG").encode()),
        ("cpc/b.cpc", cpc_text("images/b.jpg", label="ALG").encode()),
    ])
    report = validate_workspace(workspace, manual_label_codes="ACR,POR")
    assert report.label_inventory["ALG"]["point_count"] == 2
    assert report.label_inventory["ALG"]["file_count"] == 2
    assert report.label_inventory["ALG"]["status"] == "unknown"

    mapped = validate_workspace(
        workspace,
        manual_label_codes="ACR,POR",
        label_overrides={"ALG": "ACR"},
    )
    assert mapped.label_inventory["ALG"]["status"] == "mapped"
    assert mapped.summary["ready"] == 2


def test_bulk_prefix_replacement_resolves_duplicate_filename(tmp_path):
    image = image_bytes(tmp_path)
    workspace = save_uploaded_files([
        ("images/Survey-1/reef.jpg", image),
        ("images/Survey-2/reef.jpg", image),
        ("cpc/reef.cpc", cpc_text(r"D:\Legacy\Batch-A\reef.jpg").encode()),
    ])
    before = validate_workspace(workspace, manual_label_codes="ACR")
    assert before.cpc_results[0].matched_image is None
    assert "AMBIGUOUS_IMAGE_MATCH" in [issue.code for issue in before.cpc_results[0].issues]

    after = validate_workspace(
        workspace,
        manual_label_codes="ACR",
        path_rules=[{
            "type": "prefix_replace",
            "from": r"D:\Legacy\Batch-A",
            "to": "images/Survey-1",
        }],
    )
    result = after.cpc_results[0]
    assert result.matched_image == "images/Survey-1/reef.jpg"
    assert result.action_category == "ready"


def test_bulk_keep_last_rule(tmp_path):
    workspace = save_uploaded_files([
        ("images/Survey-A/reef.jpg", image_bytes(tmp_path)),
        ("cpc/reef.cpc", cpc_text(r"D:\Legacy\Nested\Survey-A\reef.jpg").encode()),
    ])
    report = validate_workspace(
        workspace,
        manual_label_codes="ACR",
        path_rules=[{"type": "keep_last", "count": 2, "prepend": "images"}],
    )
    assert report.cpc_results[0].effective_image_path == "images/Survey-A/reef.jpg"
    assert report.cpc_results[0].action_category == "ready"


def test_cpc_folder_rule_resolves_duplicate_filename(tmp_path):
    image = image_bytes(tmp_path)
    workspace = save_uploaded_files([
        ("dataset/images/Survey-A/reef.jpg", image),
        ("dataset/images/Survey-B/reef.jpg", image),
        ("dataset/cpc/Survey-A/reef.cpc", cpc_text(r"D:\Legacy\reef.jpg").encode()),
    ])
    report = validate_workspace(
        workspace,
        manual_label_codes="ACR",
        path_rules=[{"type": "cpc_folder", "cpc_root": "cpc", "image_root": "images"}],
    )
    assert report.cpc_results[0].matched_image == "dataset/images/Survey-A/reef.jpg"


def test_macos_metadata_is_ignored(tmp_path):
    image = image_bytes(tmp_path)
    workspace = save_uploaded_files([
        ("dataset/images/IMG_0039.jpg", image),
        ("dataset/annotations_cpc/IMG_0039.cpc", cpc_text(r"D:\Survey\IMG_0039.jpg").encode()),
        ("dataset/__MACOSX/dataset/annotations_cpc/._IMG_0039.cpc", b"appledouble metadata"),
        ("dataset/.DS_Store", b"metadata"),
    ])
    images, cpcs, _csvs = discover_files(workspace)
    assert len(images) == 1
    assert len(cpcs) == 1


def test_zip_ignores_macosx_and_strips_redundant_root(tmp_path):
    image = image_bytes(tmp_path)
    archive_path = tmp_path / "fiji_95_rare.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fiji_95_rare/images/IMG_0039.jpg", image)
        archive.writestr(
            "fiji_95_rare/annotations_cpc/IMG_0039.cpc",
            cpc_text(r"D:\Survey\IMG_0039.jpg"),
        )
        archive.writestr(
            "__MACOSX/fiji_95_rare/annotations_cpc/._IMG_0039.cpc",
            b"appledouble metadata",
        )
    workspace = save_uploaded_files([(archive_path.name, archive_path.read_bytes())])
    images, cpcs, _csvs = discover_files(workspace)
    assert [path.relative_to(workspace.original_files).as_posix() for path in images] == [
        "fiji_95_rare/images/IMG_0039.jpg"
    ]
    assert [path.relative_to(workspace.original_files).as_posix() for path in cpcs] == [
        "fiji_95_rare/annotations_cpc/IMG_0039.cpc"
    ]


def test_package_separates_ready_and_needs_attention_and_has_audit(tmp_path):
    workspace = save_uploaded_files([
        ("images/ready.jpg", image_bytes(tmp_path, name="ready.jpg")),
        ("cpc/ready.cpc", cpc_text(r"D:\Old\ready.jpg").encode()),
        ("cpc/missing.cpc", cpc_text(r"D:\Old\missing.jpg").encode()),
    ])
    first = validate_workspace(workspace, manual_label_codes="ACR")
    report = accept_all(workspace, first, manual_label_codes="ACR")
    package = build_prepared_package(workspace)
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert "READY/cpc/cpc/ready.cpc" in names
        assert "NEEDS_ATTENTION/cpc/cpc/missing.cpc" in names
        assert "reports/change-audit.csv" in names
        assert "reports/validation-report.html" in names
        assert "README-CORALNET-IMPORT.txt" in names
