import time
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .audit import record_event, record_exception
from .bootstrap import initialize_database
from .routers import collections, credentials, inventory, managers, settings, system


@asynccontextmanager
async def lifespan(_: FastAPI):
    for attempt in range(20):
        try:
            initialize_database()
            break
        except psycopg.OperationalError:
            if attempt == 19:
                raise
            time.sleep(1)
    yield


app = FastAPI(title="Sentinel API", version="0.1.0", lifespan=lifespan)


def _request_route(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path if isinstance(route_path, str) else request.url.path


def _request_id(request: Request) -> str:
    value = getattr(request.state, "audit_request_id", None)
    if isinstance(value, str):
        return value
    generated = uuid4().hex
    request.state.audit_request_id = generated
    return generated


@app.middleware("http")
async def internal_audit_response_middleware(request: Request, call_next):
    request_id = _request_id(request)
    try:
        response = await call_next(request)
    except Exception as exc:
        if not getattr(request.state, "audit_recorded", False):
            request.state.audit_recorded = True
            record_exception(
                exc,
                service="api",
                event_kind="unhandled_exception",
                request_id=request_id,
                route=_request_route(request),
                method=request.method,
                status_code=500,
            )
        raise
    response.headers["X-Sentinel-Audit-Request-Id"] = request_id
    if response.status_code >= 400 and not getattr(request.state, "audit_recorded", False):
        request.state.audit_recorded = True
        record_event(
            service="api",
            event_kind="http_response_error",
            request_id=request_id,
            route=_request_route(request),
            method=request.method,
            status_code=response.status_code,
            message=f"HTTP response status {response.status_code}",
        )
    return response


@app.exception_handler(HTTPException)
async def internal_audit_http_exception_handler(request: Request, exc: HTTPException):
    request.state.audit_recorded = True
    record_event(
        service="api",
        event_kind="http_exception",
        request_id=_request_id(request),
        route=_request_route(request),
        method=request.method,
        status_code=exc.status_code,
        exception_type=type(exc).__name__,
        message=exc.detail,
    )
    return await http_exception_handler(request, exc)


@app.exception_handler(StarletteHTTPException)
async def internal_audit_starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    request.state.audit_recorded = True
    record_event(
        service="api",
        event_kind="http_exception",
        request_id=_request_id(request),
        route=_request_route(request),
        method=request.method,
        status_code=exc.status_code,
        exception_type=type(exc).__name__,
        message=exc.detail,
    )
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def internal_audit_validation_exception_handler(request: Request, exc: RequestValidationError):
    request.state.audit_recorded = True
    validation_context = [
        {"location": list(error.get("loc", ())), "type": error.get("type"), "message": error.get("msg")}
        for error in exc.errors()
    ]
    record_event(
        service="api",
        event_kind="request_validation_error",
        request_id=_request_id(request),
        route=_request_route(request),
        method=request.method,
        status_code=422,
        exception_type=type(exc).__name__,
        message="Request validation failed",
        context={"errors": validation_context},
    )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def internal_audit_unhandled_exception_handler(request: Request, exc: Exception):
    if not getattr(request.state, "audit_recorded", False):
        request.state.audit_recorded = True
        record_exception(
            exc,
            service="api",
            event_kind="unhandled_exception",
            request_id=_request_id(request),
            route=_request_route(request),
            method=request.method,
            status_code=500,
        )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error."},
        headers={"X-Sentinel-Audit-Request-Id": _request_id(request)},
    )

app.include_router(system.router)
app.include_router(inventory.router)
app.include_router(managers.router)
app.include_router(credentials.router)
app.include_router(settings.router)
app.include_router(collections.router)
