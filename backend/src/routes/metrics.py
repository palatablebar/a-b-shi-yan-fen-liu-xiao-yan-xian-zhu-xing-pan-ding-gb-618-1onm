from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import CHECK_ROLES
from src.controllers import experiment_controller, metric_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.experiment import MetricCheckIn, SrmCheckIn
from src.utils.pagination import page_params

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def list_metrics(
    experiment_id: int | None = None,
    variant_code: str | None = None,
    significant: bool | None = None,
    q: str | None = None,
    analysis_version_id: int | None = None,
    active_only: bool = True,
    limit: int = Query(40),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return metric_controller.list_metrics(
        db,
        experiment_id=experiment_id,
        variant_code=variant_code,
        significant=significant,
        q=q,
        analysis_version_id=analysis_version_id,
        active_only=active_only,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/experiments/{experiment_id}/srm")
def get_srm(
    experiment_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """当前生效分析版本的 SRM 快照（只读）。"""
    return metric_controller.srm_snapshot(db, experiment_id)


@router.get("/experiments/{experiment_id}/analysis/{version_id}/metrics")
def version_metrics(
    experiment_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """查看任意历史版本的整套指标结果（含已失效版本），供追溯。"""
    return experiment_controller.version_metrics(db, experiment_id, version_id)


@router.post("/experiments/{experiment_id}/srm-check")
def run_srm(
    experiment_id: int,
    payload: SrmCheckIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CHECK_ROLES)),
) -> dict:
    """兼容入口：不脱离版本单独跑，按当前生效版本的区间整套重算后返回 SRM 快照。"""
    return metric_controller.rerun_active_version(
        db, experiment_id, principal, request, piece="srm", payload=payload.model_dump(exclude_none=True)
    )


@router.post("/experiments/{experiment_id}/metric-tests")
def run_metric_tests(
    experiment_id: int,
    payload: MetricCheckIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CHECK_ROLES)),
) -> dict:
    """兼容入口：按当前生效版本的区间整套重算后返回全部指标结果。"""
    return metric_controller.rerun_active_version(
        db, experiment_id, principal, request, piece="metrics", payload=payload.model_dump(exclude_none=True)
    )
