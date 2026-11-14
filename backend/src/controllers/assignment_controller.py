"""分流样本控制器：列表 / 渠道矩阵 / 录入。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import analysis_service, assignment_service, audit_service, experiment_service
from src.services.algorithms import exposure


def list_assignments(db: Session, experiment_id: int, analysis_version_id: int | None = None, **filters) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    version = analysis_service.active_version(db, experiment)
    if analysis_version_id is not None:
        version = analysis_service.get_version(db, experiment, analysis_version_id)
    filters["segments"] = version.segments if version else None
    filters["analysis_version_id"] = version.id if version else None
    result = assignment_service.list_assignments(db, experiment_id, **filters)
    result["analysis"] = analysis_service.serialize_version(version, experiment=experiment) if version else None
    result["interval_applied"] = bool(version and version.segments)
    return result


def matrix(
    db: Session,
    experiment_id: int,
    metric_key: str | None = None,
    analysis_version_id: int | None = None,
) -> dict:
    """样本矩阵：默认按当前生效版本的可用曝光区间过滤，区间外样本单独计数。"""
    experiment = experiment_service.get_experiment(db, experiment_id)
    variants = experiment_service.get_variants(db, experiment_id)
    version = analysis_service.active_version(db, experiment)
    if analysis_version_id is not None:
        version = analysis_service.get_version(db, experiment, analysis_version_id)
    segments = version.segments if version else None
    excluded = (
        analysis_service.out_of_interval_rows(db, experiment, segments) if segments else None
    )
    return {
        "experiment": experiment_service.serialize(experiment, variants=variants),
        "analysis": analysis_service.serialize_version(version, experiment=experiment) if version else None,
        "interval_applied": bool(segments),
        "excluded_rows": excluded,
        "totals": assignment_service.variant_totals(db, experiment, metric_key, segments=segments),
        "matrix": assignment_service.channel_matrix(db, experiment, metric_key, segments=segments),
        "daily": assignment_service.daily_series(db, experiment, metric_key, segments=segments),
        "metric_keys": assignment_service.metric_keys(db, experiment_id, segments=segments),
    }


def create_assignment(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    row = assignment_service.create_assignment(db, experiment, payload, operator_email=principal["email"])
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="assignment.create",
        entity="assignment",
        entity_id=row.id,
        detail={
            "experiment": experiment.code,
            "variant": payload["variant_code"],
            "channel": payload["channel"],
            "metric": payload["metric_key"],
            "samples": row.samples,
            "in_active_interval": (
                exposure.contains(experiment.active_interval_segments, payload["stat_date"])
                if experiment.active_analysis_version_id
                else None
            ),
            "active_analysis_version_id": experiment.active_analysis_version_id,
        },
        ip=request.client.host if request and request.client else None,
    )
    db.commit()
    return assignment_service.serialize(row)
