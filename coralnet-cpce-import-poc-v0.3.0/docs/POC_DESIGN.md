# POC design and product mapping — v0.3.0

## User problem

Legacy CPCe migrations frequently involve obsolete embedded Windows paths, reorganized image folders, duplicate filenames, incompatible image dimensions, and CPCe codes that do not exist in the destination CoralNet Source.

## Automation-first workflow

1. Upload one legacy dataset ZIP.
2. Inventory CPC files, images, labels, and ignored metadata.
3. Infer deterministic CPC-to-image relationships.
4. Present grouped safe path repairs for one-click approval.
5. Validate dimensions, scale factors, coordinates, and labels.
6. Show only exceptions requiring user action.
7. Optionally run CPCe-safe lossless JPEG optimization.
8. Generate READY, NEEDS_ATTENTION, and EXCLUDED output groups.
9. Include a complete audit and CoralNet upload instructions.

## Primary safety rule

Structural repairs may be automated only when deterministic. Scientific observations are never changed automatically.

The tool does not resize annotated images, alter coordinate values, clamp points, or guess labels.

## Technical structure

- `app.py`: FastAPI endpoints and temporary workspace lifecycle
- `cpc_parser.py`: CPC parser and writer
- `validator.py`: discovery, inference, validation, reports, and packaging
- `jpeg_optimizer.py`: lossless JPEG optimization and before/after verification
- `preannotation.py`: isolated workflow for images with no CPC annotations
- `templates/` and `static/`: guided browser UI
- `tests/`: parser, validation, packaging, JPEG, and workflow tests
