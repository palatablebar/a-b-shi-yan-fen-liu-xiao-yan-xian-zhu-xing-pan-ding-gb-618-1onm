"""分析版本控制器：登记可用曝光区间 → 整套重算发布；列出 / 查看版本。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import analysis_service, audit_service, experiment_service
from src.services.algorithms import exposure
from src.utils.errors import AppError


def list_versions(db: Session, experiment_id: int) -> dict:
    experiment_service.get_experiment(db, experiment_id)
    return analysis_service.list_versions(db, experiment_id)


def get_version(db: Session, experiment_id: int, version_id: int) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    version = analysis_service.get_version(db, experiment, version_id)
    return analysis_service.serialize_version(version, experiment=experiment)


def run_analysis(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    segments_raw = payload["segments"]
    try:
        version, created = analysis_service.register(
            db,
            experiment,
            segments_raw=segments_raw,
            note=payload.get("note") or "",
            method=payload.get("method") or "bh",
            alpha=payload.get("alpha"),
            power=payload.get("power"),
            created_by=principal["email"],
        )
    except AppError:
        db.rollback()
        raise
    if payload.get("reuse_existing") is False and not created:
        db.rollback()
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": "同一组可用曝光区间已有分析版本，重跑复用该版本，不新增版本",
                "analysis_version_id": version.id,
                "revision": version.revision,
                "interval_key": version.interval_key,
            },
        )
    try:
        result = analysis_service.run_and_publish(
            db,
            experiment,
            version=version,
            operator_email=principal["email"],
            variant_code=payload.get("variant_code"),
        )
    except AppError as exc:
        # run_and_publish 用 SAVEPOINT 回滚了本次写入；这里只把失败原因记到版本上，
        # 实验的 active 指针与 SRM 快照保持上一版，不整体回滚掉本次登记的区间。
        db.refresh(experiment)
        failed = analysis_service.get_version(db, experiment, version.id)
        db.refresh(failed)
        analysis_service.mark_failure(db, failed, exc.detail.get("reason") or exc.message)
        db.refresh(experiment)
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
    # 成功路径下 experiment 可能被 SAVEPOINT 刷新掉状态，重新读一次再序列化
    db.refresh(experiment)

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
            "interval_label": result["version"]["interval_label"],
            "created": created,
            "reused": not created,
            "srm_alarm": result["srm"]["alarm"],
            "metrics_count": result["summary"]["metrics_count"],
            "significant_count": result["summary"]["significant_count"],
        },
        ip=request.client.host if request and request.client else None,
    )
    db.commit()
    return {
        "created": created,
        "reused": not created,
        "version": result["version"],
        "srm": result["srm"],
        "metric": result["metric_result"],
        "summary": result["summary"],
    }
