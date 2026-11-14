"""决策控制器。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import analysis_service, audit_service, decision_service, experiment_service
from src.services.algorithms import exposure
from src.utils.errors import AppError


def list_decisions(db: Session, **filters) -> dict:
    return decision_service.list_decisions(db, **filters)


def create_decision(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    version = analysis_service.require_active_version(db, experiment)
    requested = payload.get("analysis_version_id")
    if requested is not None and not analysis_service.version_is_current(db, experiment, int(requested)):
        requested_version = analysis_service.get_version(db, experiment, int(requested))
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": "只能对当前生效的分析版本出决策；该版本已失效，请按当前版本重新分析",
                "requested_version_id": requested_version.id,
                "requested_revision": requested_version.revision,
                "requested_status": requested_version.status,
                "active_version_id": version.id,
                "active_revision": version.revision,
            },
        )
    decision = decision_service.create(db, experiment, payload, decided_by=principal["email"])
    experiment.status = "finished"
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="decision.create",
        entity="decision",
        entity_id=decision.id,
        detail={
            "experiment": experiment.code,
            "verdict": decision.verdict,
            "analysis_version_id": decision.analysis_version_id,
            "analysis_revision": decision.analysis_version_revision,
            "interval_key": decision.interval_key,
            "interval_label": exposure.interval_label(version.segments),
        },
        ip=request.client.host if request and request.client else None,
    )
    db.commit()
    return decision_service.serialize(decision, experiment, version)
