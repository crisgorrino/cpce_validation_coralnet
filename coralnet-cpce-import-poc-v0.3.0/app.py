from __future__ import annotations

import errno
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from jpeg_optimizer import resolve_jpegtran
from validator import (
    WORKSPACES,
    build_prepared_package,
    create_workspace,
    ensure_free_space,
    expand_uploaded_archives,
    safe_relative_path,
    validate_workspace,
)

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="CoralNet Legacy CPCe Migration Tool", version="0.3.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.exception_handler(OSError)
async def handle_os_error(_request: Request, exc: OSError):
    if exc.errno == errno.ENOSPC:
        return JSONResponse(
            status_code=507,
            content={
                "detail": (
                    f"The validator ran out of local disk space. {exc}. "
                    "Delete old cpce-poc temporary folders or free disk space, restart the server, and retry."
                )
            },
        )
    return JSONResponse(status_code=500, content={"detail": f"Local file error: {exc}"})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


async def save_uploads_to_workspace(files: list[UploadFile]):
    dataset_id, workspace = create_workspace()
    try:
        for upload in files:
            filename = upload.filename or "unnamed"
            relative = safe_relative_path(filename)
            destination = workspace.original_files / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as stream:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    ensure_free_space(workspace.root, len(chunk))
                    stream.write(chunk)
            await upload.close()
        expand_uploaded_archives(workspace)
        return dataset_id, workspace
    except Exception:
        WORKSPACES.pop(dataset_id, None)
        shutil.rmtree(workspace.root, ignore_errors=True)
        raise


@app.post("/api/validate")
async def validate(
    files: list[UploadFile] = File(...),
    dataset_token: str = Form(""),
    label_codes: str = Form(""),
    label_mode: str = Form("id_only"),
    image_overrides: str = Form("{}"),
    label_overrides: str = Form("{}"),
    path_rules: str = Form("[]"),
    accepted_suggestions: str = Form("[]"),
    optimize_jpegs: bool = Form(False),
    preannotation_enabled: bool = Form(False),
    preannotation_max_dimension: int = Form(4096),
    preannotation_quality: int = Form(95),
):
    try:
        image_override_data = json.loads(image_overrides or "{}")
        label_override_data = json.loads(label_overrides or "{}")
        path_rule_data = json.loads(path_rules or "[]")
        accepted_suggestion_data = json.loads(accepted_suggestions or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid mapping JSON: {exc}") from exc

    if not isinstance(path_rule_data, list):
        raise HTTPException(status_code=400, detail="Bulk path rules must be a JSON list.")
    if not isinstance(accepted_suggestion_data, list):
        raise HTTPException(status_code=400, detail="Accepted suggestions must be a JSON list.")

    if dataset_token:
        workspace = WORKSPACES.get(dataset_token)
        if workspace is None:
            raise HTTPException(status_code=404, detail="Dataset session expired; upload the files again.")
    else:
        real_files = [upload for upload in files if upload.filename != "placeholder.ignore"]
        if not real_files:
            raise HTTPException(status_code=400, detail="Upload at least one file or ZIP archive.")
        dataset_token, workspace = await save_uploads_to_workspace(real_files)

    report = validate_workspace(
        workspace,
        manual_label_codes=label_codes,
        label_mode=label_mode,
        image_overrides=image_override_data,
        label_overrides=label_override_data,
        path_rules=path_rule_data,
        accepted_suggestions=accepted_suggestion_data,
        optimize_jpegs=optimize_jpegs,
        preannotation_enabled=preannotation_enabled,
        preannotation_max_dimension=preannotation_max_dimension,
        preannotation_quality=preannotation_quality,
    )
    payload = report.to_dict()
    payload["dataset_token"] = dataset_token
    return JSONResponse(payload)


@app.get("/api/package/{dataset_token}")
def package(dataset_token: str):
    workspace = WORKSPACES.get(dataset_token)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Dataset session expired; upload the files again.")
    package_path = build_prepared_package(workspace)
    return FileResponse(
        package_path,
        media_type="application/zip",
        filename="coralnet-cpce-migration-package.zip",
    )


@app.get("/api/capabilities")
def capabilities():
    jpegtran = resolve_jpegtran()
    return {
        "version": "0.3.0",
        "jpegtran_available": bool(jpegtran),
        "jpegtran_path": jpegtran,
        "lossless_jpeg_optimization": bool(jpegtran),
    }


@app.get("/sample-data.zip")
def sample_data():
    path = BASE_DIR / "sample-data.zip"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Sample archive not found.")
    return FileResponse(path, media_type="application/zip", filename="cpce-poc-sample-data.zip")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "0.3.0",
        "jpegtran_available": bool(resolve_jpegtran()),
    }
