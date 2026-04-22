from __future__ import annotations

from importlib.resources import files

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from xuanshu import __version__
from xuanshu.config.settings import DashboardRuntimeSettings
from xuanshu.dashboard.service import DashboardService, PostgresDashboardReader, RedisDashboardReader


def build_dashboard_service(settings: DashboardRuntimeSettings | None = None) -> DashboardService:
    runtime_settings = settings or DashboardRuntimeSettings()
    return DashboardService(
        runtime_reader=RedisDashboardReader(str(runtime_settings.redis_url)),
        history_reader=PostgresDashboardReader(str(runtime_settings.postgres_dsn)),
        symbols=runtime_settings.okx_symbols,
        app_version=__version__,
    )


def create_app(service: DashboardService | None = None) -> FastAPI:
    dashboard_service = service or build_dashboard_service()
    app = FastAPI(title="Xuanshu Dashboard", version=__version__, docs_url=None, redoc_url=None)
    static_dir = files("xuanshu.dashboard").joinpath("static")
    app.mount("/xuanshu/static", StaticFiles(directory=str(static_dir)), name="xuanshu-static")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/xuanshu/")

    @app.get("/xuanshu", include_in_schema=False)
    def xuanshu_no_slash() -> RedirectResponse:
        return RedirectResponse(url="/xuanshu/")

    @app.get("/xuanshu/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        index_path = static_dir.joinpath("index.html")
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    @app.get("/xuanshu/api/overview")
    def overview() -> dict[str, object]:
        return dashboard_service.overview()

    @app.get("/xuanshu/api/equity-curve")
    def equity_curve(range: str = Query(default="24h", pattern="^(24h|7d|30d|all)$")) -> dict[str, object]:
        return dashboard_service.equity_curve(range_key=range)

    @app.get("/xuanshu/api/actions")
    def actions(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
        return {"actions": dashboard_service.actions(limit=limit)}

    @app.get("/xuanshu/healthz")
    def healthz() -> dict[str, object]:
        return dashboard_service.health()

    return app
