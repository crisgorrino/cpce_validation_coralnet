# v0.3.0 Implementation and Validation Design

## Implementation approach

### Dataset discovery

The tool recursively discovers CPC, image, and CSV files. It ignores common operating-system artifacts, including:

- `__MACOSX`
- `._*`
- `.DS_Store`
- `Thumbs.db`
- `desktop.ini`
- `$RECYCLE.BIN`
- Spotlight metadata
- Temporary Office files

ZIP extraction blocks parent-path traversal, applies an uncompressed-size limit, and removes one redundant same-name wrapper folder.

### Image relationship inference

Each CPC file is parsed before repair. The embedded image path remains the source of truth for the expected filename.

Matching strategies are deterministic and ordered from strongest to weakest:

1. Exact relative path
2. Case-insensitive exact path
3. Unique case-sensitive path suffix
4. Unique filename
5. Unique case-insensitive suffix or filename
6. Unique `.jpg` / `.jpeg` stem
7. Unique CPC filename stem

Fuzzy names are suggestions only and are never automatically selected.

When a deterministic match changes the embedded CPC path, the result is grouped into a user-approvable safe fix. The package excludes the CPC from `READY` until that fix is approved.

### Scientific compatibility validation

For each matched image:

- Parse CPC image width and height as integers
- Read actual pixel width and height
- Calculate width and height scale factors
- Require equal, positive integer scale factors
- Convert every CPC point into pixel coordinates using the same scale
- Reject points outside the image boundary

No coordinate repair is performed.

### Label validation

The tool aggregates every unique CPCe code and counts:

- Total annotation points
- Number of CPC files
- Destination label status
- Effective mapped code

Mappings are applied only after user confirmation.

### Package generation

The package uses a copy of each parsed CPC object. It never mutates uploaded originals.

Files are separated into:

- `READY`: fully validated and approved
- `NEEDS_ATTENTION`: unresolved mappings or missing information
- `EXCLUDED`: corrupted or scientifically incompatible files

A complete audit records approved path, label, optimization, and pre-annotation changes.

## CPCe-safe JPEG optimization

### Dependency

`jpegtran` from libjpeg-turbo.

The program is discovered through:

1. Explicit function argument
2. `JPEGTRAN_BIN`
3. System `PATH`
4. Project `tools/` directory
5. Common Homebrew and libjpeg-turbo paths

### Command

```text
jpegtran -copy all -optimize INPUT
```

Standard output is captured into a temporary candidate file. The original is never used as the output target.

### Pre-optimization checks

- File extension is `.jpg` or `.jpeg`, case-insensitive
- CPC embedded basename exactly equals the real filename, including capitalization and extension
- Pillow identifies and verifies the file as JPEG
- Record width, height, mode, EXIF orientation, progressive status, file size, file SHA-256, EXIF SHA-256, ICC SHA-256, and decoded-pixel SHA-256

### Post-optimization acceptance rules

The candidate must satisfy all of the following:

- JPEG verification succeeds
- Filename is unchanged
- Width and height are unchanged
- EXIF orientation is unchanged
- Decoded pixels are byte-identical using the same Pillow decoder
- EXIF marker checksum is unchanged
- ICC profile checksum is unchanged
- Candidate is smaller than the original

If any check fails, the candidate is deleted and the original is preserved.

A baseline/progressive scan-mode change is reported as a warning, not a failure, provided all required checks—including decoded pixel identity—pass.

### Reported metrics

- Original size
- Candidate size
- Bytes saved
- Percentage reduction
- Original and output checksums
- Dimensions
- Orientation
- Validation errors and warnings

## Separate pre-annotation workflow

This optional workflow is limited to JPEGs that:

- Have no matched CPC file
- Do not share a case-insensitive basename with any CPC image reference

It can:

- Apply EXIF orientation to the pixels
- Set output orientation to 1
- Resize proportionally to a configured maximum long edge
- Preserve ICC data where possible
- Write a new canonical JPEG into `PRE_ANNOTATION_ONLY`

It never processes an image already connected—or potentially connected—to a CPC file.

## Error and warning messages

### Blocking scientific errors

- `NON_INTEGER_SCALE_FACTOR`: image dimensions do not preserve CPC coordinates
- `POINT_OUTSIDE_IMAGE`: annotation point maps outside valid pixels
- `INVALID_CPC_DIMENSIONS`: CPC line 1 dimensions are invalid
- `CPC_PARSE_ERROR`: CPC structure cannot be parsed
- `MULTIPLE_CPCS_FOR_IMAGE`: more than one CPC is assigned to one image

### User-review errors

- `IMAGE_NOT_FOUND`: no image matches after all deterministic searches
- `AMBIGUOUS_IMAGE_MATCH`: multiple equally valid image candidates
- `UNKNOWN_LABEL`: CPCe code is absent from the destination labelset
- `NO_LABELSET_PROVIDED`: label validation has not been completed

### Safe repair messages

- `SAFE_PATH_REPAIR_AVAILABLE`: a deterministic path rewrite is waiting for approval

### JPEG optimization warnings

- `JPEGTRAN_NOT_AVAILABLE`: original images are preserved
- `JPEG_OPTIMIZATION_REJECTED`: candidate failed validation; original preserved
- `JPEG_OPTIMIZATION_MEMORY_WARNING`: file bytes may shrink, but decoded memory does not

## UI changes

The interface now follows five steps:

1. Upload
2. Validation options
3. Approve safe fixes
4. Resolve exceptions
5. Review and download

The results table defaults to files requiring action and provides category filters.

Manual path-rule controls remain available under an Advanced section rather than being the primary workflow.

## Automated tests

The test suite verifies:

- Safe unique-filename inference remains pending until approval
- Exact path needs no approval
- Dimension mismatch is classified as not safely repairable
- Labels are aggregated and mapped once
- Duplicate basenames remain ambiguous
- Existing bulk rules continue to work
- ZIP metadata is ignored
- Packages split READY and NEEDS_ATTENTION content
- Audit and HTML reports are generated
- Uppercase `.JPG` filenames are preserved
- EXIF orientation is preserved through lossless optimization
- Progressive JPEGs decode identically
- Corrupted JPEGs are rejected
- Filename case mismatch blocks optimization
- Pixel-array identity is required
- Annotated and unannotated workflows are strictly separated

## Edge cases

### Uppercase `.JPG`

Supported. The real filename and suffix are preserved exactly.

### EXIF orientation

The lossless workflow never auto-orients. It requires the orientation tag to remain unchanged.

The separate pre-annotation workflow may apply orientation and writes the output orientation as 1.

### Progressive JPEGs

Supported. Progressive status is recorded before and after. Pixel identity and metadata preservation remain mandatory.

### Corrupted JPEGs

Rejected before optimization. The original remains available in the dataset and the validation report identifies the file.

### Duplicate filenames

Automatic selection occurs only when path context produces one unique image. Otherwise, manual confirmation is required.

### CPC references a missing image

The report includes the CPC file, original embedded path, expected filename, all searches performed, and a downloadable missing-image CSV.

### Candidate is larger after jpegtran

The candidate is discarded and the original is preserved.

### EXIF or ICC changes

The candidate is rejected even when its decoded pixels are identical.
