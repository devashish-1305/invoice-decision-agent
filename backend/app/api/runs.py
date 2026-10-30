"""POST /api/runs (upload a PDF or pick a sample), GET /api/runs (history + KPIs), GET /api/runs/{id},
GET /api/runs/{id}/page.png."""
import pymupdf
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.api import views
from app.api.deps import AppState, get_session, get_state
from app.api.execution import execute_run
from app.api.samples import load_samples
from app.config import Settings
from app.db import models as m
from app.db import repository as repo

router = APIRouter()


def bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def check_pdf(pdf: bytes, settings: Settings) -> None:
    """Reject a bad upload with a clear message BEFORE any run is created."""
    if not pdf:
        raise bad_request("The file is empty.")
    if len(pdf) > settings.max_upload_bytes:
        raise bad_request(f"The file is larger than {settings.max_upload_bytes // (1024 * 1024)} MB.")
    if not pdf.startswith(b"%PDF-"):
        raise bad_request("Only PDF files are accepted.")
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            pages = doc.page_count
    except Exception:  # noqa: BLE001
        raise bad_request("That file could not be read as a PDF.")
    if pages == 0:
        raise bad_request("The PDF has no pages.")
    if pages > settings.max_pages:
        raise bad_request(f"The PDF has {pages} pages; the limit is {settings.max_pages}.")


async def read_request(request: Request, state: AppState) -> tuple[bytes, str]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            raise bad_request("The request body is not valid JSON.")
        code = body.get("sample") if isinstance(body, dict) else None
        if not code:
            raise bad_request('Send a PDF as multipart form data (field "file") or JSON like {"sample": "E1"}.')
        card = next((c for c in load_samples(state) if c["code"] == code), None)
        if card is None:
            raise bad_request(f"Unknown sample '{code}'.")
        return (state.settings.data_dir / "invoices" / card["file"]).read_bytes(), card["file"]
    if content_type.startswith("multipart/form-data"):
        upload = (await request.form()).get("file")
        if upload is None or not hasattr(upload, "read"):
            raise bad_request('No file received. Send the PDF in a form field named "file".')
        return await upload.read(state.settings.max_upload_bytes + 1), upload.filename or "upload.pdf"
    raise bad_request('Send a PDF as multipart form data (field "file") or JSON like {"sample": "E1"}.')


@router.post("/runs", status_code=202)
async def start_run(request: Request, background: BackgroundTasks, state: AppState = Depends(get_state)):
    pdf, filename = await read_request(request, state)
    check_pdf(pdf, state.settings)

    def create_queued_run() -> int:
        with state.session_factory() as session:
            run_id = repo.create_run(session, filename, pdf, status="queued")
            session.commit()                      # the run exists before we answer
            return run_id

    run_id = await run_in_threadpool(create_queued_run)
    background.add_task(execute_run, state, run_id, pdf, filename)
    return {"run_id": run_id, "status": "queued"}


@router.get("/runs")
def list_runs(session=Depends(get_session)):
    return views.run_list(session)


@router.get("/runs/{run_id}")
def get_run(run_id: int, session=Depends(get_session)):
    detail = views.run_detail(session, run_id)
    if detail is None:
        raise HTTPException(404, "Run not found")
    return detail


@router.get("/runs/{run_id}/page.png")
def page_image(run_id: int, session=Depends(get_session)):
    run = session.get(m.Run, run_id)
    if run is None or not run.pdf:
        raise HTTPException(404, "No document for this run")
    with pymupdf.open(stream=run.pdf, filetype="pdf") as doc:
        png = doc[0].get_pixmap(dpi=110).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
