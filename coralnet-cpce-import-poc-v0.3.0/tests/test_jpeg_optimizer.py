import os
from pathlib import Path

from PIL import Image

from jpeg_optimizer import inspect_jpeg, optimize_jpeg_losslessly
from validator import save_uploaded_files, validate_workspace


def make_fake_jpegtran(tmp_path: Path) -> Path:
    script = tmp_path / "jpegtran"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "data = pathlib.Path(sys.argv[-1]).read_bytes()\n"
        "end = data.rfind(b'\\xff\\xd9')\n"
        "sys.stdout.buffer.write(data[:end+2] if end >= 0 else data)\n"
    )
    script.chmod(0o755)
    return script


def make_jpeg(tmp_path: Path, name="IMG_0001.JPG", progressive=False, orientation=None) -> Path:
    path = tmp_path / name
    image = Image.new("RGB", (80, 60), (20, 90, 160))
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    image.save(path, "JPEG", quality=88, progressive=progressive, exif=exif)
    path.write_bytes(path.read_bytes() + b"TRAILING-PADDING" * 100)
    return path


def test_lossless_optimizer_preserves_uppercase_name_orientation_and_pixels(tmp_path):
    source = make_jpeg(tmp_path, progressive=True, orientation=6)
    fake = make_fake_jpegtran(tmp_path)
    output_root = tmp_path / "out"
    result = optimize_jpeg_losslessly(
        source,
        "images/IMG_0001.JPG",
        output_root,
        expected_cpc_filename="IMG_0001.JPG",
        jpegtran_bin=str(fake),
    )
    assert result.status == "optimized"
    assert result.bytes_saved > 0
    assert result.original.width == result.optimized.width
    assert result.original.height == result.optimized.height
    assert result.original.exif_orientation == result.optimized.exif_orientation == 6
    assert result.original.decoded_pixels_sha256 == result.optimized.decoded_pixels_sha256
    assert (output_root / "images/IMG_0001.JPG").exists()


def test_optimizer_rejects_cpc_filename_case_mismatch(tmp_path):
    source = make_jpeg(tmp_path, name="IMG_0001.JPG")
    fake = make_fake_jpegtran(tmp_path)
    result = optimize_jpeg_losslessly(
        source,
        "images/IMG_0001.JPG",
        tmp_path / "out",
        expected_cpc_filename="img_0001.jpg",
        jpegtran_bin=str(fake),
    )
    assert result.status == "rejected"
    assert "CPC_FILENAME_MISMATCH" in result.errors


def test_corrupt_jpeg_is_rejected(tmp_path):
    source = tmp_path / "bad.jpg"
    source.write_bytes(b"not a jpeg")
    fake = make_fake_jpegtran(tmp_path)
    result = optimize_jpeg_losslessly(
        source,
        "images/bad.jpg",
        tmp_path / "out",
        expected_cpc_filename="bad.jpg",
        jpegtran_bin=str(fake),
    )
    assert result.status == "rejected"
    assert "INVALID_JPEG" in result.errors


def test_validator_optimization_and_preannotation_are_separate(tmp_path, monkeypatch):
    fake = make_fake_jpegtran(tmp_path)
    monkeypatch.setenv("JPEGTRAN_BIN", str(fake))

    annotated = make_jpeg(tmp_path, name="annotated.jpg")
    orphan = tmp_path / "orphan.jpg"
    exif = Image.Exif(); exif[274] = 6
    Image.new("RGB", (900, 600), (180, 80, 20)).save(orphan, "JPEG", exif=exif)

    cpc = (
        '"codes.txt","images/annotated.jpg",1200,900,800,600\r\n'
        '0,0\r\n0,900\r\n1200,900\r\n1200,0\r\n'
        '1\r\n150,150\r\n"1","ACR","Notes",""\r\n'
    )
    workspace = save_uploaded_files([
        ("images/annotated.jpg", annotated.read_bytes()),
        ("images/orphan.jpg", orphan.read_bytes()),
        ("cpc/annotated.cpc", cpc.encode()),
    ])
    report = validate_workspace(
        workspace,
        manual_label_codes="ACR",
        optimize_jpegs=True,
        preannotation_enabled=True,
        preannotation_max_dimension=400,
    )
    assert report.optimization["optimized"] == 1
    assert report.pre_annotation["prepared"] == 1
    assert "images/annotated.jpg" in workspace.optimized_lookup
    assert "images/orphan.jpg" in workspace.preannotation_results
    assert "images/annotated.jpg" not in workspace.preannotation_results
