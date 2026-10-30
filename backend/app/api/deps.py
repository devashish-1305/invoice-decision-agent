"""What every route needs: settings, the database, a way to make an LLM client, and "today"."""
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from fastapi import Depends, Request

from app.config import Settings


@dataclass
class AppState:
    settings: Settings
    engine: Any
    session_factory: Any                # writer sessions: runs, reset (BEGIN IMMEDIATE)
    read_session_factory: Any           # read-only sessions: GET endpoints and the SSE poll
    llm_factory: Callable[[], Any]      # a fresh LLM client per run (tests inject a fake)
    today: Callable[[], date] = date.today


def get_state(request: Request) -> AppState:
    return request.app.state.ctx


def get_session(state: AppState = Depends(get_state)):
    with state.read_session_factory() as session:
        yield session
