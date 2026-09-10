"""Localhost-only HTTP demonstration over synthetic prepared temperature guidance."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from mesoforge.application.point_forecast import (
    PreparedPointForecast,
    UnsupportedCoordinateError,
    prepare_demo_files,
)


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "data_kind": "synthetic_demonstration",
            "notice": "Synthetic demonstration data; not a current weather forecast.",
            "error": message,
        },
    )


def create_app(directory: Path) -> FastAPI:
    """Read and close prepared files once, before accepting any forecast request."""
    prepared = PreparedPointForecast.from_directory(directory)
    app = FastAPI(
        title="MesoForge synthetic temperature demonstration",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error("Provide numeric lat and lon query parameters within the demo region.", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error(str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return _error("Synthetic demonstration could not calculate this request.", 500)

    @app.get("/forecast", response_model=None)
    def forecast(lat: float, lon: float) -> dict[str, Any] | JSONResponse:
        try:
            return prepared.forecast(latitude=lat, longitude=lon)
        except UnsupportedCoordinateError as exc:
            return _error(str(exc), 422)

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "mesoforge-synthetic-temperature-demo",
        help="Prepared-file directory; an empty directory gets two synthetic files at startup.",
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    prepare_demo_files(args.data_dir)
    uvicorn.run(create_app(args.data_dir), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
