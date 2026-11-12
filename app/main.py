import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import api, jobs, web
from app.config import get_settings
from app.db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    st = get_settings()
    if st.is_production and st.dev_auth_email:
        raise RuntimeError("DEV_AUTH_EMAIL must not be set in production")
    init_db()
    if st.run_jobs:
        jobs.start()
    yield
    jobs.stop()


app = FastAPI(title="GBC ecobee manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(api.router)
app.include_router(web.router)


@app.exception_handler(StarletteHTTPException)
async def html_errors(request: Request, exc: StarletteHTTPException):
    """The UI is plain forms, so a failed POST should land on a readable page, not JSON."""
    wants_html = "text/html" in request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or not wants_html:
        return await http_exception_handler(request, exc)
    from html import escape
    detail = escape(str(exc.detail))
    title = {401: "You need to sign in", 403: "You can't change this",
             404: "That page doesn't exist"}.get(exc.status_code, "That didn't work")
    body = (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<title>{title}</title><link rel=stylesheet href=/static/app.css>"
            f"<main class=error-page><h1>{title}</h1>"
            f"<div class='msg error' role=alert>{detail[:1].upper()}{detail[1:]}</div>"
            f"<p><a href='javascript:history.back()'>← Go back and try again</a>"
            f" · <a href='/'>Rooms</a></p></main></html>")
    return HTMLResponse(body, status_code=exc.status_code, headers=exc.headers)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
