"""Localhost prepared-temperature calculation and immutable issued-version retrieval."""

from __future__ import annotations

import argparse
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from mesoforge.application.issuance import (
    read_issued_forecast,
    select_issued_forecast_hours,
    validate_hour_selection,
)
from mesoforge.application.observation_preview import preview_observation_match
from mesoforge.application.point_forecast import (
    PreparedPointForecast,
    prepare_demo_files,
)
from mesoforge.application.prepared_local_grid import PreparedLocalGrids
from mesoforge.application.spatial_coverage import CoverageRequiredError, UnsupportedCoordinateError
from mesoforge.application.spatial_preparation import PreparedRegions, load_prepared
from mesoforge.application.weather_conditions import preview_weather_conditions
from mesoforge.application.weather_periods import preview_weather_periods
from mesoforge.application.weather_transitions import (
    preview_weather_transitions,
    validate_display_timezone,
)
from mesoforge.common.errors import NotFound
from mesoforge.forecasting.conditions import SCOPES, ConditionsPreviewUnavailableError


def _error(
    message: str,
    status_code: int,
    prepared: PreparedPointForecast | PreparedRegions | PreparedLocalGrids,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "data_kind": prepared.data_kind,
            "notice": prepared.notice,
            "error": message,
        },
    )


def _hour_query_error() -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_issued_forecast_hour_query",
                "message": (
                    "Provide finite geographic lat/lon and timezone-aware ISO start_valid_time "
                    "and end_valid_time, with start before end (end excluded)."
                ),
            }
        },
    )


def create_app(directory: Path) -> FastAPI:
    """Read and close prepared files once, before accepting any forecast request."""
    prepared = (
        PreparedLocalGrids.from_directory(directory)
        if (directory / "local-grids.json").is_file()
        else load_prepared(directory)
    )
    app = FastAPI(
        title="MesoForge prepared temperature demonstration",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path == "/issued-forecast-hours":
            return _hour_query_error()
        return _error(
            "Provide numeric lat and lon query parameters with valid geographic ranges.",
            422,
            prepared,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error(str(exc.detail), exc.status_code, prepared)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return _error(
            "Prepared temperature demonstration could not calculate this request.", 500, prepared
        )

    @app.get("/forecast", response_model=None)
    def forecast(lat: float, lon: float) -> dict[str, Any] | JSONResponse:
        try:
            regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
            if any(
                isinstance(region, PreparedPointForecast)
                and region._surface_configuration is not None
                for region in regions
            ):
                # Native -> local regridding stays in explicit preparation/batch work.
                # HTTP serves the retained grid, never builds a domain during a GET.
                from mesoforge.application.spatial_coverage import validate_coordinate

                validate_coordinate(lat, lon)
                raise CoverageRequiredError(
                    "Prepare local surface grids before HTTP with "
                    "mesoforge.application.prepared_local_grid, then serve its output directory"
                )
            return prepared.forecast(latitude=lat, longitude=lon)
        except UnsupportedCoordinateError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "data_kind": prepared.data_kind,
                    "notice": prepared.notice,
                    "error": str(exc),
                    "code": "unsupported_coordinate",
                },
            )
        except CoverageRequiredError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "data_kind": prepared.data_kind,
                    "notice": prepared.notice,
                    "error": str(exc),
                    "code": "coverage_required",
                },
            )

    @app.get("/issued-forecasts/{issued_forecast_id}", response_model=None)
    def issued_forecast(issued_forecast_id: str) -> dict[str, Any] | JSONResponse:
        # Validate here so saved-version errors never inherit prepared-data labels.
        try:
            identifier = UUID(issued_forecast_id)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_issued_forecast_id",
                        "message": "Provide an issued-forecast ID in UUID format.",
                    }
                },
            )
        try:
            return read_issued_forecast(identifier)
        except NotFound:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "issued_forecast_not_found",
                        "message": "No saved issued forecast exists for this ID.",
                    }
                },
            )
        except Exception:
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "issued_forecast_read_failed",
                        "message": "Could not read and verify the saved issued forecast.",
                    }
                },
            )

    @app.get("/issued-forecasts/{issued_forecast_id}/conditions", response_model=None)
    def weather_conditions(
        issued_forecast_id: str, scope: str = "point"
    ) -> dict[str, Any] | JSONResponse:
        try:
            identifier = UUID(issued_forecast_id)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_issued_forecast_id",
                        "message": "Provide an issued-forecast ID in UUID format.",
                    }
                },
            )
        if scope not in SCOPES:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_conditions_scope",
                        "message": "Provide scope=point (default), scope=editable or scope=grid.",
                    }
                },
            )
        try:
            return preview_weather_conditions(identifier, scope=scope)
        except NotFound:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "issued_forecast_not_found",
                        "message": "No saved issued forecast exists for this ID.",
                    }
                },
            )
        except ConditionsPreviewUnavailableError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "conditions_preview_unavailable", "message": str(exc)}},
            )
        except Exception:
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "conditions_preview_failed",
                        "message": "Could not read and verify the saved condition-preview inputs.",
                    }
                },
            )

    @app.get("/issued-forecasts/{issued_forecast_id}/conditions/transitions", response_model=None)
    def weather_transitions(
        issued_forecast_id: str, display_timezone: str | None = None
    ) -> dict[str, Any] | JSONResponse:
        try:
            identifier = UUID(issued_forecast_id)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_issued_forecast_id",
                        "message": "Provide an issued-forecast ID in UUID format.",
                    }
                },
            )
        if display_timezone is not None:
            try:
                validate_display_timezone(display_timezone)
            except ValueError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "invalid_display_timezone",
                            "message": "Provide a resolvable IANA display_timezone, e.g. UTC.",
                        }
                    },
                )
        try:
            return preview_weather_transitions(identifier, display_timezone=display_timezone)
        except NotFound:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "issued_forecast_not_found",
                        "message": "No saved issued forecast exists for this ID.",
                    }
                },
            )
        except ConditionsPreviewUnavailableError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "transitions_preview_unavailable", "message": str(exc)}},
            )
        except Exception:
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "transitions_preview_failed",
                        "message": "Could not read and verify the saved transition-preview inputs.",
                    }
                },
            )

    @app.get("/issued-forecasts/{issued_forecast_id}/conditions/periods", response_model=None)
    def weather_periods(
        issued_forecast_id: str, display_timezone: str | None = None
    ) -> dict[str, Any] | JSONResponse:
        try:
            identifier = UUID(issued_forecast_id)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_issued_forecast_id",
                        "message": "Provide an issued-forecast ID in UUID format.",
                    }
                },
            )
        if display_timezone is not None:
            try:
                validate_display_timezone(display_timezone)
            except ValueError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "invalid_display_timezone",
                            "message": "Provide a resolvable IANA display_timezone, e.g. UTC.",
                        }
                    },
                )
        try:
            return preview_weather_periods(identifier, display_timezone=display_timezone)
        except NotFound:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "issued_forecast_not_found",
                        "message": "No saved issued forecast exists for this ID.",
                    }
                },
            )
        except ConditionsPreviewUnavailableError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "periods_preview_unavailable", "message": str(exc)}},
            )
        except Exception:
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "periods_preview_failed",
                        "message": "Could not read and verify the saved period-summary inputs.",
                    }
                },
            )

    @app.get("/issued-forecasts/{issued_forecast_id}/observation-match", response_model=None)
    def observation_match(
        issued_forecast_id: str, valid_time: str | None = None
    ) -> dict[str, Any] | JSONResponse:
        try:
            identifier = UUID(issued_forecast_id)
            instant = datetime.fromisoformat(valid_time or "")
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError("valid_time must include a timezone")
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_observation_match_query",
                        "message": (
                            "Provide an issued-forecast UUID and timezone-aware ISO valid_time."
                        ),
                    }
                },
            )
        try:
            return preview_observation_match(identifier, instant)
        except NotFound:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "issued_forecast_hour_not_found",
                        "message": "No saved forecast hour exists for this ID and valid time.",
                    }
                },
            )
        except Exception:
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "observation_match_failed",
                        "message": (
                            "Could not read and verify the retained observation-match inputs."
                        ),
                    }
                },
            )

    @app.get("/issued-forecast-hours", response_model=None)
    def issued_forecast_hours(
        lat: float, lon: float, start_valid_time: str, end_valid_time: str
    ) -> dict[str, Any] | JSONResponse:
        try:
            start, end = (
                datetime.fromisoformat(start_valid_time),
                datetime.fromisoformat(end_valid_time),
            )
            validate_hour_selection(lat, lon, start, end)
        except ValueError:
            return _hour_query_error()
        try:
            return select_issued_forecast_hours(
                latitude=lat, longitude=lon, start_valid_time=start, end_valid_time=end
            )
        except Exception:
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "issued_forecast_hour_selection_failed",
                        "message": "Could not read and verify the saved forecast hours.",
                    }
                },
            )

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Retained local-grid or legacy prepared directory; omitted selects synthetic data.",
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.data_dir is None:
        args.data_dir = Path(tempfile.gettempdir()) / "mesoforge-synthetic-temperature-demo"
        prepare_demo_files(args.data_dir)
    uvicorn.run(create_app(args.data_dir), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
