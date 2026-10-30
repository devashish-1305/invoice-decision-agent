"""FastAPI app: the JSON API under /api and, in production, the built React app from frontend/dist.

One command serves everything after `npm run build`:   uvicorn app.main:app --port 8000
(The Vite dev server and its /api proxy are for local development only.)
"""
import dataclasses

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import reference, runs, samples, stream
from app.api.deps import AppState
from app.api.execution import recover_interrupted_runs
from app.config import Settings, get_settings
from app.db.seed import seed_if_missing
from app.db.session import init_db, make_engine, make_session_factory
from app.llm.client import LLMClient


def create_app(settings: Settings | None = None, llm_factory=None, today=None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)                          # writer: BEGIN IMMEDIATE
    read_engine = make_engine(settings.database_url, immediate=False)    # reader: never takes the write lock
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        seed_if_missing(session, settings)       # startup seeds only if missing; history is preserved

    app = FastAPI(title="Invoice Decision Agent")
    state = AppState(settings=settings, engine=engine, session_factory=factory,
                     read_session_factory=make_session_factory(read_engine),
                     llm_factory=llm_factory or (lambda: LLMClient(settings)))
    if today:
        state.today = today
    app.state.ctx = state
    recover_interrupted_runs(state)     # runs left "running" by a previous process become failed + Hold

    for module in (samples, runs, stream, reference):
        app.include_router(module.router, prefix="/api")
    mount_frontend(app, settings.frontend_dist)
    return app


def mount_frontend(app: FastAPI, dist) -> None:
    """Serve frontend/dist: static assets, and index.html for every other non-/api path (SPA fallback)."""
    index = dist / "index.html"
    if not index.exists():
        return  # API-only until the frontend is built
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not found")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)


_app: FastAPI | None = None


def __getattr__(name: str):
    """`uvicorn app.main:app` looks up `app` here. Built on first use, so importing this module (tests) has no side effects."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
