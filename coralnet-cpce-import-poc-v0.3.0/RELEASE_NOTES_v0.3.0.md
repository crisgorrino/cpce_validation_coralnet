# Release notes — v0.3.0

## Added

- Automatic deterministic image-match inference
- Grouped one-click safe path repairs
- Exception-only result filtering
- Dataset-level label inventory and mappings
- Plain-language dimension and coordinate diagnostics
- READY / NEEDS_ATTENTION / EXCLUDED package structure
- HTML validation report and complete change audit
- CPCe-safe lossless JPEG optimization using libjpeg-turbo `jpegtran`
- Before/after JPEG validation for dimensions, orientation, EXIF, ICC, filename, checksums, and decoded pixels
- Optional isolated pre-annotation orientation/resize workflow for images with no CPC annotations
- Expanded macOS and Windows metadata filtering
- ZIP traversal and expanded-size safety checks

## Changed

- Manual path rules are now an advanced fallback rather than the primary workflow.
- Safe inferred path fixes require user approval before CPC files are placed in READY output.
- Results are classified by the user action required rather than only error/warning status.
- Package generation no longer presents unresolved files alongside ready files.

## Safety boundaries

- Annotated images are never resized, cropped, rotated, flipped, auto-oriented, renamed, or re-exported with a quality setting.
- Annotation coordinates are never scaled, clamped, or moved.
- Label mappings are never guessed or applied without confirmation.
- Failed or non-beneficial JPEG optimizations always preserve the original image.
