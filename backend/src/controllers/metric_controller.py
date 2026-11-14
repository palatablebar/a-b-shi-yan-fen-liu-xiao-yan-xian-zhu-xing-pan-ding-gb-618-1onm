"""算法控制器：分析版本读取 + 兼容入口（按当前生效版本区间整套重算）。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import analysis_service, audit_service, experiment_service, metric_service, srm_service
from src.services.algorithms import exposure
from src.utils.errors import AppError


def list_metrics(db: Session, **filters) -> dict:
    return metric_service.list_metrics(db, **filters)


def srm_snapshot(db: Session, experiment_id: int) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    return srm_service.snapshot(experiment)


def rerun_active_version(
    db: Session,
    experiment_id: int,
    principal: dict,
    request: Request,
    *,
    piece: str,
    payload: dict,
) -> dict:
    """兼容旧按钮：必须是已登记的生效版本，区间不变地整套重算；绝不按整段累计。"""
    experiment = experiment_service.get_experiment(db, experiment_id)
    version = analysis_service.require_active_version(db, experiment)
    if payload.get("alpha") is not None:
        version.alpha = payload["alpha"]
    if payload.get("power") is not None:
        version.power_target = payload["power"]
    if payload.get("method") is not None:
        version.method = payload["method"]
    try:
        result = analysis_service.run_and_publish(
            db,
            experiment,
            version=version,
            operator_email=principal["email"],
            variant_code=payload.get("variant_code"),
        )
    except AppError as exc:
        db.refresh(experiment)
        failed = analysis_service.get_version(db, experiment, version.id)
        db.refresh(failed)
        analysis_service.mark_failure(db, failed, exc.detail.get("reason") or exc.message)
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="analysis.failed",
            entity="analysis_version",
            entity_id=failed.id,
            detail={
                "experiment": experiment.code,
                "revision": failed.revision,
                "interval_label": exposure.interval_label(failed.segments),
                "reason": exc.detail.get("reason") or exc.message,
                "code": exc.code,
            },
            ip=request.client.host if request and request.client else None,
        )
        db.commit()
        raise
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="analysis.publish",
        entity="analysis_version",
        entity_id=result["version"]["id"],
        detail={
            "experiment": experiment.code,
            "revision": result["version"]["revision"],
            "interval_key": result["version"]["interval_key"],
            "reused": True,
            "via": piece,
            "metrics_count": result["summary"]["metrics_count"],
            "significant_count": result["summary"]["significant_count"],
        },
        ip=request.client.host if request and request.client else None,
    )
    db.commit()
    if piece == "srm":
        return result["srm"]
    return result["metric_result"]
