"""Explicit SPA routes: frontend fallback never claims an API namespace."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles


BUILD_DIRECTORY = Path(__file__).resolve().parent / "frontend" / "dist"
CLIENT_PATHS = ("/login", "/register", "/passenger", "/driver", "/account")
REVALIDATE = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}


class ViteAssets(StaticFiles):
    """Only Vite's generated, content-hashed assets belong in /assets."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206, 304):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def register_frontend(app: FastAPI, directory: Path = BUILD_DIRECTORY) -> None:
    """Register at import time; restart Uvicorn after building/removing assets.

    Missing builds preserve backend-only development. No root StaticFiles mount
    or catch-all is used, so unknown API paths retain FastAPI's JSON 404/405.
    """
    index = directory / "index.html"

    def html():
        if not index.is_file():
            raise HTTPException(status_code=404, detail="Frontend build not found")
        return FileResponse(index, media_type="text/html", headers=REVALIDATE)

    def root():
        if not index.is_file():
            return {"message": "welcome to ride share api"}
        return html()

    app.add_api_route("/", root, methods=["GET", "HEAD"], include_in_schema=False)
    for path in CLIENT_PATHS:
        app.add_api_route(path, html, methods=["GET", "HEAD"], include_in_schema=False)

    # These two unhashed public files must revalidate, like index.html.
    def public_file(name: str):
        def serve():
            file = directory / name
            if not file.is_file():
                raise HTTPException(status_code=404, detail="Not found")
            return FileResponse(file, headers=REVALIDATE)
        return serve

    for name in ("favicon.svg", "journey.svg"):
        app.add_api_route(f"/{name}", public_file(name), methods=["GET", "HEAD"], include_in_schema=False)
    if (directory / "assets").is_dir():
        app.mount("/assets", ViteAssets(directory=directory / "assets"), name="frontend-assets")
