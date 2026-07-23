# CoralNet Legacy CPCe Migration Tool — v0.3.0

A local proof of concept for validating, safely repairing, and packaging legacy CPCe datasets before uploading them into CoralNet.

## Product purpose

This is a one-way migration tool:

```text
Legacy CPCe images + .cpc annotations
→ validate and approve safe repairs
→ download CoralNet-ready files and reports
→ upload images first, then CPC files, into CoralNet
```

It does not log in to CoralNet or modify a live Source.

## Highest-value improvements in v0.3.0

### 1. Automatic image matching and path-fix inference

The validator tries, in order:

- Exact relative path
- Normalized Windows/macOS/Linux path
- Longest unique path suffix
- Unique filename
- Case-insensitive unique filename
- `.jpg` / `.jpeg` extension variant
- CPC filename stem

When a safe deterministic match requires changing the path embedded in a CPC file, the UI presents one grouped suggestion such as:

> Match 478 images by unique filename

The user approves the safe fix once instead of writing a path rule manually.

### 2. Exception-only review

Results are grouped into:

- `Ready`
- `Automatic repair`
- `Needs review`
- `Cannot safely repair`

The table defaults to files needing action.

### 3. Dataset-level label mapping

Every CPCe code is counted once across the dataset. The report shows:

- Point count
- Number of CPC files using the code
- Exact destination match
- User-confirmed mapping
- Unknown status and suggestions

One mapping applies to all matching annotations.

### 4. Plain-language scientific diagnostics

Image compatibility errors show:

- CPCe coordinate dimensions
- Uploaded image dimensions
- Width and height scale factors
- Aspect ratios
- A plain-language explanation of likely causes

The tool never automatically resizes images or moves/clamps CPC annotation points.

### 5. CoralNet migration package and audit report

The ZIP output contains:

```text
READY/
  images/
  cpc/
NEEDS_ATTENTION/
  cpc/
EXCLUDED/
  cpc/
PRE_ANNOTATION_ONLY/
  images/
reports/
  validation-report.html
  validation-report.json
  validation-summary.csv
  change-audit.csv
  missing-images.csv
  label-inventory.csv
  label-mapping.csv
  jpeg-optimization.csv
README-CORALNET-IMPORT.txt
```

Original files are never overwritten.

## CPCe-safe lossless JPEG optimization

When enabled, the tool uses `jpegtran` from libjpeg-turbo:

```bash
jpegtran -copy all -optimize input.jpg > optimized.jpg
```

The application runs the command without shell redirection and writes to a temporary candidate file.

Before and after optimization it verifies:

- Valid JPEG decoding
- Exact filename and extension, including capitalization
- Pixel width and height
- EXIF orientation
- EXIF marker checksum
- ICC profile checksum
- File checksum
- Decoded pixel checksum using the same Pillow JPEG decoder

The optimized candidate is rejected unless the decoded pixel arrays are identical and all required structural checks pass. The original is also retained whenever the candidate is not smaller.

### Important warning

Lossless JPEG optimization may reduce upload bytes, but it does **not** reduce:

- Pixel count
- Decoded memory requirements
- CoralNet processing cost associated with high-resolution dimensions

## Separate pre-annotation image workflow

An optional advanced workflow can normalize EXIF orientation and resize JPEGs that do not have CPC annotations.

This workflow:

- Is disabled by default
- Writes to `PRE_ANNOTATION_ONLY/`
- Never runs on a matched image
- Never runs on an image whose filename is referenced by any CPC file
- Is intended to create a new canonical image before new CPCe points are generated

It is intentionally separate from the lossless workflow because it changes pixels and dimensions.

## Required dependencies

### Python

- Python 3.11 or later
- FastAPI
- Uvicorn
- Pillow
- python-multipart
- Jinja2

Install with:

```bash
python -m pip install -r requirements.txt
```

### Optional lossless optimization dependency

`jpegtran` from libjpeg-turbo is required only for CPCe-safe JPEG optimization.

macOS with Homebrew:

```bash
brew install jpeg-turbo
```

Windows:

- Install an official libjpeg-turbo build containing `jpegtran.exe`
- Put it on `PATH`, set `JPEGTRAN_BIN`, or place it in `tools/jpegtran.exe`

Linux/Docker:

```bash
apt-get install libjpeg-turbo-progs
```

The Dockerfile installs this package automatically.

## Run locally

### macOS

```bash
cd coralnet-cpce-import-poc-v0.3.0
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Or open `run-mac.command`.

### Windows Command Prompt

```bat
cd coralnet-cpce-import-poc-v0.3.0
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
py -m uvicorn app:app --reload
```

Or run `run-windows.bat`.

Open:

```text
http://127.0.0.1:8000
```

## Recommended user workflow

1. Upload one ZIP containing the legacy images and CPC files.
2. Add the destination CoralNet Source short codes or include a labelset CSV.
3. Enable CPCe-safe JPEG optimization only when `jpegtran` is available.
4. Analyze the dataset.
5. Approve grouped safe path fixes.
6. Map unknown labels once.
7. Review missing, ambiguous, or scientifically incompatible files.
8. Download the migration package.
9. Upload `READY/images` to CoralNet first.
10. Upload `READY/cpc` through CoralNet’s CPCe annotation upload workflow.

## Safe and unsafe automation boundary

### Safe structural changes

- Normalize path separators
- Rewrite obsolete image paths after deterministic matching
- Correct path capitalization in prepared CPC copies
- Apply user-confirmed label mappings
- Remove BOMs during CPC rewrite
- Ignore macOS and Windows system metadata
- Optimize JPEG entropy coding losslessly after full verification

### Never automated for annotated images

- Resize
- Crop
- Rotate
- Flip
- Auto-orient
- Rename
- Change extension
- Re-export at a JPEG quality value
- Scale annotation coordinates
- Clamp or move annotation points
- Guess between duplicate images
- Guess scientific label mappings

## Automated tests

```bash
python -m pytest -q
```

Current result:

```text
17 passed
```

Coverage includes:

- Unique-filename repair suggestions and approval
- Exact-path matching
- Scale-factor failures
- Dataset-level label mapping
- Duplicate filenames
- Bulk prefix and folder rules
- macOS ZIP metadata
- READY / NEEDS_ATTENTION packaging
- Uppercase `.JPG`
- EXIF orientation
- Progressive JPEGs
- Corrupted JPEGs
- Filename capitalization mismatch
- Pixel-identity validation
- Separation of annotated optimization and unannotated preprocessing

See `docs/IMPLEMENTATION_AND_VALIDATION.md` for the detailed design.
