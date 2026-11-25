"""Local FastAPI host for the Streamable HTTP transport."""

from contextlib import asynccontextmanager
from secrets import compare_digest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP


def create_app(server: FastMCP, token: str) -> FastAPI:
    if not token:
        raise ValueError("An MCP bearer token is required")
    expected = token.encode()
    transport = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app):
        async with server.session_manager.run():
            yield

    app = FastAPI(title="Vast.ai MCP", lifespan=lifespan, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path != "/healthz":
            scheme, _, value = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not compare_digest(value.encode(), expected):
                return JSONResponse({"detail": "Invalid MCP bearer token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    app.mount("/", transport)
    return app
