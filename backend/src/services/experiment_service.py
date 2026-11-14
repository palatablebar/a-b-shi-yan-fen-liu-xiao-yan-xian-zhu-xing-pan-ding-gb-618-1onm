"""实验登记服务：实验 + 变体的读写与序列化。"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CORRECTION_LABELS,
    EXPERIMENT_STATUS_LABELS,
    METRIC_LABELS,
    SRM_STATUS_LABELS,
    VERDICT_LABELS,
)
from src.models.analysis_version import AnalysisVersion
from src.models.assignment import Assignment
from src.models.experiment import Experiment
from src.models.metric_result import MetricResult
from src.models.variant import Variant
from src.services import analysis_service, decision_service
from src.services.algorithms import exposure
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_date, iso_utc


def serialize_variant(variant: Variant) -> dict:
    return {
        "id": variant.id,
        "code": variant.code,
        "name": variant.name,
        "ratio": variant.ratio,
        "ratio_pct": round(variant.ratio * 100, 2),
        "is_control": variant.is_control,
        "note": variant.note,
    }


def serialize(experiment: Experiment, *, variants: list[Variant] | None = None) -> dict:
    return {
        "id": experiment.id,
        "code": experiment.code,
        "name": experiment.name,
        "hypothesis": experiment.hypothesis,
        "owner": experiment.owner,
        "primary_metric": experiment.primary_metric,
        "primary_metric_label": METRIC_LABELS.get(experiment.primary_metric, experiment.primary_metric),
        "status": experiment.status,
        "status_label": EXPERIMENT_STATUS_LABELS.get(experiment.status, experiment.status),
        "baseline_rate": experiment.baseline_rate,
        "planned_mde": experiment.planned_mde,
        "alpha": experiment.alpha,
        "power_target": experiment.power_target,
        "traffic_note": experiment.traffic_note,
        "started_on": iso_date(experiment.started_on),
        "ended_on": iso_date(experiment.ended_on),
        "srm": {
            "status": experiment.srm_status,
            "status_label": SRM_STATUS_LABELS.get(experiment.srm_status, experiment.srm_status),
            "chi2": experiment.srm_chi2,
            "df": experiment.srm_df,
            "p_value": experiment.srm_p_value,
            "checked_at": iso_utc(experiment.srm_checked_at),
            "alarm": experiment.has_srm_alarm,
            "required_per_variant": experiment.required_per_variant,
            "power_at_current": experiment.power_at_current,
            "mde_at_current": experiment.mde_at_current,
            "payload": experiment.srm_payload or {},
        },
        "analysis": {
            "version_id": experiment.active_analysis_version_id,
            "revision": experiment.active_analysis_revision,
            "publication": experiment.active_analysis_publication,
            "interval_key": experiment.active_interval_key,
            "interval_segments": experiment.active_interval_segments or [],
            "interval_label": exposure.interval_label(experiment.active_interval_segments),
            "interval_days": experiment.active_interval_days,
            "published_at": iso_utc(experiment.active_analysis_published_at),
            "is_active": experiment.active_analysis_version_id is not None,
        },
        "variants": [serialize_variant(item) for item in (variants or [])],
        "created_at": iso_utc(experiment.created_at),
        "updated_at": iso_utc(experiment.updated_at),
    }


def list_experiments(
    db: Session,
    *,
    q: str | None = None,
    status: str | None = None,
    primary_metric: str | None = None,
    srm_alarm: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(Experiment)
    count_stmt = select(func.count()).select_from(Experiment)
    if q:
        like = f"%{q.strip()}%"
        clause = or_(Experiment.name.ilike(like), Experiment.code.ilike(like), Experiment.hypothesis.ilike(like))
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    if status:
        stmt = stmt.where(Experiment.status == status)
        count_stmt = count_stmt.where(Experiment.status == status)
    if primary_metric:
        stmt = stmt.where(Experiment.primary_metric == primary_metric)
        count_stmt = count_stmt.where(Experiment.primary_metric == primary_metric)
    if srm_alarm is not None:
        stmt = stmt.where(Experiment.has_srm_alarm.is_(srm_alarm))
        count_stmt = count_stmt.where(Experiment.has_srm_alarm.is_(srm_alarm))
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Experiment.id).limit(limit).offset(offset)).all()
    variant_map = _variants_by_experiment(db, [row.id for row in rows])
    return {
        "items": [serialize(row, variants=variant_map.get(row.id, [])) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _variants_by_experiment(db: Session, ids: list[int]) -> dict[int, list[Variant]]:
    if not ids:
        return {}
    rows = db.scalars(select(Variant).where(Variant.experiment_id.in_(ids)).order_by(Variant.sort_order, Variant.id)).all()
    grouped: dict[int, list[Variant]] = {}
    for row in rows:
        grouped.setdefault(row.experiment_id, []).append(row)
    return grouped


def get_experiment(db: Session, experiment_id: int) -> Experiment:
    experiment = db.get(Experiment, experiment_id)
    if experiment is None:
        raise not_found("experiment", experiment_id)
    return experiment


def get_variants(db: Session, experiment_id: int) -> list[Variant]:
    return list(
        db.scalars(
            select(Variant).where(Variant.experiment_id == experiment_id).order_by(Variant.sort_order, Variant.id)
        ).all()
    )


def detail(db: Session, experiment_id: int, *, analysis_version_id: int | None = None) -> dict:
    experiment = get_experiment(db, experiment_id)
    variants = get_variants(db, experiment_id)
    decision = decision_service.get_by_experiment(db, experiment_id)
    decision_history = decision_service.history_by_experiment(db, experiment_id)
    version = analysis_service.active_version(db, experiment)
    selected_version = version
    if analysis_version_id is not None:
        selected_version = analysis_service.get_version(db, experiment, analysis_version_id)
    version_id = selected_version.id if selected_version else None
    if version_id is not None:
        metrics = db.scalars(
            select(MetricResult)
            .where(MetricResult.analysis_version_id == version_id)
            .order_by(MetricResult.metric_key)
        ).all()
    else:
        metrics = []
    # 主指标口径的样本合计：有生效版本时按版本区间统计，避免把整段累计
    sample_stmt = select(func.sum(Assignment.samples), func.sum(Assignment.conversions), func.count()).where(
        Assignment.experiment_id == experiment_id, Assignment.metric_key == experiment.primary_metric
    )
    segments = selected_version.segments if selected_version else None
    interval = analysis_service.assignment_date_filter(segments)
    if interval is not None:
        sample_stmt = sample_stmt.where(interval)
    sample_rows = db.execute(sample_stmt).one()
    payload = serialize(experiment, variants=variants)
    payload["sample_total"] = int(sample_rows[0] or 0)
    payload["assignment_rows"] = int(sample_rows[2] or 0)
    payload["selected_analysis_version_id"] = version_id
    payload["selected_analysis"] = (
        analysis_service.serialize_version(selected_version, experiment=experiment)
        if selected_version
        else None
    )
    payload["metrics"] = [
        {
            "metric_key": row.metric_key,
            "metric_name": row.metric_name,
            "variant_code": row.variant_code,
            "p_value": row.p_value,
            "q_value": row.q_value,
            "lift": row.lift,
            "significant": row.significant,
            "verdict": row.verdict,
            "verdict_label": VERDICT_LABELS.get(row.verdict, row.verdict),
            "method": row.method,
            "method_label": CORRECTION_LABELS.get(row.method, row.method),
            "is_primary": row.is_primary,
            "analysis_version_id": row.analysis_version_id,
        }
        for row in metrics
    ]
    decision_version = db.get(AnalysisVersion, decision.analysis_version_id) if decision else None
    payload["decision"] = (
        decision_service.serialize(decision, experiment, decision_version) if decision else None
    )
    payload["decision_history"] = [
        decision_service.serialize(
            row, experiment, db.get(AnalysisVersion, row.analysis_version_id)
        )
        for row in decision_history
    ]
    return payload


def create_experiment(db: Session, payload: dict, *, owner_default: str) -> Experiment:
    exists = db.scalar(select(Experiment).where(Experiment.code == payload["code"]))
    if exists is not None:
        raise AppError("CONFLICT", 409, {"code": payload["code"], "entity": "experiment"})
    experiment = Experiment(
        code=payload["code"],
        name=payload["name"],
        hypothesis=payload.get("hypothesis") or "",
        owner=payload.get("owner") or owner_default,
        primary_metric=payload.get("primary_metric") or "order_rate",
        status=payload.get("status") or "running",
        baseline_rate=float(payload.get("baseline_rate") or 0.1),
        planned_mde=float(payload.get("planned_mde") or 0.01),
        alpha=float(payload.get("alpha") or 0.05),
        power_target=float(payload.get("power_target") or 0.8),
        traffic_note=payload.get("traffic_note"),
        started_on=payload.get("started_on"),
        ended_on=payload.get("ended_on"),
    )
    db.add(experiment)
    db.flush()
    for index, item in enumerate(payload.get("variants") or []):
        db.add(
            Variant(
                experiment_id=experiment.id,
                code=item["code"],
                name=item["name"],
                ratio=float(item["ratio"]),
                is_control=bool(item.get("is_control")),
                note=item.get("note"),
                sort_order=index,
            )
        )
    db.flush()
    return experiment


def update_experiment(db: Session, experiment: Experiment, payload: dict) -> Experiment:
    for field in ("name", "hypothesis", "owner", "status", "baseline_rate", "planned_mde", "alpha", "power_target", "traffic_note"):
        if field in payload and payload[field] is not None:
            setattr(experiment, field, payload[field])
    db.flush()
    return experiment
