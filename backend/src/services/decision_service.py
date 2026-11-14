"""决策服务：一次实验一个结论，且必须绑定当前生效的分析版本。

规则：
- 出决策前必须有 active 分析版本（否则 409 NO_ANALYSIS_VERSION）；
- 决策落库写入 analysis_version_id / revision / interval_key，导出与审计可追溯；
- 同一版本重复决策 → 409；版本失效后旧决策保留为历史（stale=true），
  可对新的生效版本重新出决策；页面与导出都不会把失效决策当作有效结论。
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import VERDICT_LABELS
from src.models.analysis_version import AnalysisVersion
from src.models.decision import Decision
from src.models.experiment import Experiment
from src.services import analysis_service
from src.services.algorithms import exposure
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, utcnow


def serialize(row: Decision, experiment: Experiment | None = None, version: AnalysisVersion | None = None) -> dict:
    stale = analysis_service.decision_is_stale(row, version)
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "experiment_code": experiment.code if experiment else "",
        "experiment_name": experiment.name if experiment else "",
        "analysis_version_id": row.analysis_version_id,
        "analysis_revision": row.analysis_version_revision,
        "analysis_version_publication": row.analysis_version_publication,
        "interval_key": row.interval_key,
        "interval_segments": (version.segments if version else []) or [],
        "interval_label": exposure.interval_label(version.segments) if version else "",
        "analysis_status": version.status if version else "missing",
        "publication_current": bool(version and version.publication_count == row.analysis_version_publication),
        "analysis_status_label": analysis_service.STATUS_LABELS.get(version.status, "已失效") if version else "已失效",
        "stale": stale,
        "verdict": row.verdict,
        "verdict_label": VERDICT_LABELS.get(row.verdict, row.verdict),
        "rationale": row.rationale,
        "decided_by": row.decided_by,
        "decided_at": iso_utc(row.decided_at),
    }


def get_by_experiment(db: Session, experiment_id: int) -> Decision | None:
    """该实验「当前有效」的决策：必须绑定当前生效版本的当前发布轮次。"""
    experiment = db.get(Experiment, experiment_id)
    if experiment is None or experiment.active_analysis_version_id is None:
        return None
    version = db.get(AnalysisVersion, experiment.active_analysis_version_id)
    if version is None or version.status != "active":
        return None
    return db.scalar(
        select(Decision).where(
            Decision.experiment_id == experiment_id,
            Decision.analysis_version_id == version.id,
            Decision.analysis_version_publication == version.publication_count,
        )
    )


def history_by_experiment(db: Session, experiment_id: int) -> list[Decision]:
    return list(
        db.scalars(
            select(Decision).where(Decision.experiment_id == experiment_id).order_by(Decision.id.desc())
        ).all()
    )


def create(db: Session, experiment: Experiment, payload: dict, *, decided_by: str) -> Decision:
    version = analysis_service.require_active_version(db, experiment)
    existing = db.scalar(
        select(Decision).where(
            Decision.experiment_id == experiment.id,
            Decision.analysis_version_id == version.id,
            Decision.analysis_version_publication == version.publication_count,
        )
    )
    if existing is not None:
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": "该分析版本的这次发布已有决策，自然键 (experiment_id, analysis_version_id, publication) 唯一",
                "existing_id": existing.id,
                "existing_analysis_version_id": existing.analysis_version_id,
                "existing_analysis_revision": existing.analysis_version_revision,
                "existing_interval_label": exposure.interval_label(version.segments),
                "stale": False,
            },
        )
    row = Decision(
        experiment_id=experiment.id,
        analysis_version_id=version.id,
        analysis_version_revision=version.revision,
        analysis_version_publication=version.publication_count,
        interval_key=version.interval_key,
        verdict=payload["verdict"],
        rationale=payload["rationale"],
        decided_by=decided_by,
        decided_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def list_decisions(
    db: Session,
    *,
    verdict: str | None = None,
    q: str | None = None,
    limit: int = 40,
    offset: int = 0,
) -> dict:
    stmt = select(Decision)
    count_stmt = select(func.count()).select_from(Decision)
    if verdict:
        stmt = stmt.where(Decision.verdict == verdict)
        count_stmt = count_stmt.where(Decision.verdict == verdict)
    if q:
        like = f"%{q.strip()}%"
        clause = or_(Decision.rationale.ilike(like), Decision.decided_by.ilike(like))
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Decision.decided_at.desc(), Decision.id.desc()).limit(limit).offset(offset)).all()
    experiments = {
        item.id: item
        for item in db.scalars(select(Experiment).where(Experiment.id.in_([row.experiment_id for row in rows]))).all()
    } if rows else {}
    versions = {
        item.id: item
        for item in db.scalars(
            select(AnalysisVersion).where(AnalysisVersion.id.in_([row.analysis_version_id for row in rows]))
        ).all()
    } if rows else {}
    return {
        "items": [
            serialize(row, experiments.get(row.experiment_id), versions.get(row.analysis_version_id))
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
