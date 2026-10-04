"""FastAPI 应用入口：HTTP 路由与装配。"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from .db import Database
from .models import (
    LicenseCreate,
    LicenseView,
    RevocationView,
    RevokeCreate,
    VerificationCreate,
    VerificationView,
    normalize_dt,
)
from . import service
from .seed import seed_demo_data

DEFAULT_DB_PATH = "./data/festival.db"


def create_app(db_path: Optional[str] = None, seed_demo: Optional[bool] = None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    if seed_demo is None:
        seed_demo = os.environ.get("SEED_DEMO_DATA", "").lower() in ("1", "true", "yes", "on")

    db = Database(db_path)
    if seed_demo and seed_demo_data(db):
        print(f"[init] 已写入演示授权数据（db={db_path}）")

    app = FastAPI(
        title="电影节放映许可核验服务",
        description="录入放映授权、按幂等键核验场次准入、支持撤销与全程追溯。",
        version="1.0.0",
    )
    app.state.db = db

    def get_db() -> Database:
        return app.state.db

    @app.exception_handler(service.IdempotencyConflictError)
    async def idempotency_conflict_handler(_: Request, exc: service.IdempotencyConflictError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": str(exc),
                "code": "IDEMPOTENCY_CONFLICT",
                "idempotency_key": exc.key,
            },
        )

    @app.exception_handler(service.LicenseNotFoundError)
    async def license_not_found_handler(_: Request, exc: service.LicenseNotFoundError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"授权不存在: {exc}", "code": "LICENSE_NOT_FOUND"},
        )

    # ---------- 基础 ----------

    @app.get("/", summary="服务信息")
    def root():
        return {
            "service": "film-festival-license-verifier",
            "docs": "/docs",
            "endpoints": ["/licenses", "/verifications", "/health"],
        }

    @app.get("/health", summary="健康检查")
    def health():
        return {"status": "ok"}

    # ---------- 授权管理 ----------

    @app.post("/licenses", response_model=LicenseView,
              status_code=status.HTTP_201_CREATED, summary="录入放映授权")
    def create_license(body: LicenseCreate, db: Database = Depends(get_db)):
        return service.create_license(
            db,
            film_title=body.film_title,
            venue=body.venue,
            valid_from=normalize_dt(body.valid_from),
            valid_to=normalize_dt(body.valid_to),
            max_sessions=body.max_sessions,
        )

    @app.get("/licenses", response_model=list[LicenseView], summary="授权列表（含剩余额度）")
    def list_licenses(
        film_title: Optional[str] = Query(default=None),
        venue: Optional[str] = Query(default=None),
        db: Database = Depends(get_db),
    ):
        return service.list_licenses(db, film_title=film_title, venue=venue)

    @app.get("/licenses/{license_id}", response_model=LicenseView, summary="授权详情")
    def get_license(license_id: int, db: Database = Depends(get_db)):
        return service.license_view(db, license_id)

    @app.post("/licenses/{license_id}/revoke", response_model=RevocationView,
              summary="撤销授权（仅阻止后续核验，已核准场次保留）")
    def revoke_license(license_id: int, body: RevokeCreate, db: Database = Depends(get_db)):
        return service.revoke_license(db, license_id, body.reason)

    # ---------- 核验 ----------

    @app.post("/verifications", response_model=VerificationView,
              summary="核验放映场次（幂等）")
    def verify(body: VerificationCreate, response: Response, db: Database = Depends(get_db)):
        record, replayed = service.verify_screening(
            db,
            film_title=body.film_title,
            venue=body.venue,
            start_time_iso=normalize_dt(body.start_time),
            idempotency_key=body.idempotency_key,
        )
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
        return record

    @app.get("/verifications", response_model=list[VerificationView],
             summary="核验记录列表（可追溯）")
    def list_verifications(
        film_title: Optional[str] = Query(default=None),
        venue: Optional[str] = Query(default=None),
        license_id: Optional[int] = Query(default=None),
        result: Optional[str] = Query(default=None, pattern="^(approved|rejected)$"),
        db: Database = Depends(get_db),
    ):
        return service.list_verifications(
            db, film_title=film_title, venue=venue, license_id=license_id, result=result
        )

    @app.get("/verifications/{idempotency_key}", response_model=VerificationView,
             summary="按幂等键查询核验记录")
    def get_verification(idempotency_key: str, db: Database = Depends(get_db)):
        record = service.get_verification(db, idempotency_key)
        if record is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": f"核验记录不存在: {idempotency_key}",
                         "code": "VERIFICATION_NOT_FOUND"},
            )
        return record

    return app


app = create_app()
