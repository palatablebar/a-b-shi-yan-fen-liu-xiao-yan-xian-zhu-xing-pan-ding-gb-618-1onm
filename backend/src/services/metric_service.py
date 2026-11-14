"""算法 B 的落库层：在可用曝光区间内生成对照/实验组汇总 → 双比例检验 + 多重校正。

结果按分析版本落库：同一实验的不同版本各保留整套指标结果；同一版本重跑就地覆盖。
对外查询默认只看当前生效版本（除非显式指定 analysis_version_id）。
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import CORRECTION_LABELS, METRIC_LABELS, VERDICT_LABELS
from src.models.analysis_version import AnalysisVersion
from src.models.assignment import Assignment
from src.models.experiment import Experiment
from src.models.metric_result import MetricResult
from src.services import analysis_service, variant_service
from src.services.algorithms import metric_test as metric_algorithm
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, utcnow


def serialize(row: MetricResult, *, version: AnalysisVersion | None = None) -> dict:
    payload = {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "analysis_version_id": row.analysis_version_id,
        "variant_code": row.variant_code,
        "variant_name": row.variant_name,
        "metric_key": row.metric_key,
        "metric_name": row.metric_name,
        "is_primary": row.is_primary,
        "method": row.method,
        "method_label": CORRECTION_LABELS.get(row.method, row.method),
        "control_n": row.control_n,
        "control_conversions": row.control_conversions,
        "variant_n": row.variant_n,
        "variant_conversions": row.variant_conversions,
        "control_rate": row.control_rate,
        "variant_rate": row.variant_rate,
        "diff": row.diff,
        "z": row.z,
        "p_value": row.p_value,
        "q_value": row.q_value,
        "q_bh": row.q_bh,
        "q_bonferroni": row.q_bonferroni,
        "ci_low": row.ci_low,
        "ci_high": row.ci_high,
        "lift": row.lift,
        "lift_ci_low": row.lift_ci_low,
        "lift_ci_high": row.lift_ci_high,
        "significant": row.significant,
        "verdict": row.verdict,
        "verdict_label": VERDICT_LABELS.get(row.verdict, row.verdict),
        "required_n": row.required_n,
        "power_at_current": row.power_at_current,
        "alpha": row.alpha,
        "computed_at": iso_utc(row.computed_at),
        "computed_by": row.computed_by,
    }
    if version is not None:
        payload["analysis_revision"] = version.revision
        payload["analysis_status"] = version.status
        payload["interval_key"] = version.interval_key
        payload["interval_segments"] = version.segments or []
    return payload


def _totals(
    db: Session,
    experiment_id: int,
    variant_id: int,
    metric_key: str,
    segments: list[dict] | None = None,
) -> tuple[int, int]:
    stmt = select(
        func.coalesce(func.sum(Assignment.samples), 0),
        func.coalesce(func.sum(Assignment.conversions), 0),
    ).where(
        Assignment.experiment_id == experiment_id,
        Assignment.variant_id == variant_id,
        Assignment.metric_key == metric_key,
    )
    interval = analysis_service.assignment_date_filter(segments)
    if interval is not None:
        stmt = stmt.where(interval)
    row = db.execute(stmt).one()
    return int(row[0] or 0), int(row[1] or 0)


def calculate(
    db: Session,
    experiment: Experiment,
    segments: list[dict],
    *,
    method: str = "bh",
    alpha: float | None = None,
    power: float | None = None,
    variant_code: str | None = None,
) -> dict:
    """区间内跑全部指标检验（不写库），返回每个变体的结果集合。"""
    control = variant_service.control_variant(db, experiment.id)
    treatments = [
        item
        for item in variant_service.list_variants(db, experiment.id)
        if not item.is_control and (variant_code is None or item.code == variant_code)
    ]
    if not treatments:
        raise AppError("NOT_FOUND", 404, {"entity": "variant", "code": variant_code})

    interval = analysis_service.assignment_date_filter(segments)
    metric_stmt = select(Assignment.metric_key).where(Assignment.experiment_id == experiment.id)
    if interval is not None:
        metric_stmt = metric_stmt.where(interval)
    metric_keys = [
        row[0]
        for row in db.execute(metric_stmt.group_by(Assignment.metric_key).order_by(Assignment.metric_key)).all()
    ]
    if not metric_keys:
        analysis_service.guard_no_exposure(experiment, segments)

    results: list[dict] = []
    for treatment in treatments:
        metrics = []
        for metric_key in metric_keys:
            n2, c2 = _totals(db, experiment.id, control.id, metric_key, segments)
            n1, c1 = _totals(db, experiment.id, treatment.id, metric_key, segments)
            if n1 <= 0 or n2 <= 0:
                continue
            metrics.append(
                {
                    "metric_key": metric_key,
                    "metric_name": METRIC_LABELS.get(metric_key, metric_key),
                    "is_primary": metric_key == experiment.primary_metric,
                    "control": {"n": n2, "conversions": c2},
                    "variant": {"code": treatment.code, "name": treatment.name, "n": n1, "conversions": c1},
                }
            )
        if not metrics:
            raise AppError(
                "NO_EXPOSURE",
                422,
                {
                    "reason": f"变体 {treatment.code} 在登记的可用曝光区间内没有可用的分流样本",
                    "experiment": experiment.code,
                    "variant": treatment.code,
                },
            )
        try:
            outcome = metric_algorithm.run_metric_tests(
                metrics,
                method=method,
                alpha=alpha if alpha is not None else experiment.alpha,
                power=power if power is not None else experiment.power_target,
            )
        except ValueError as exc:
            raise AppError("VALIDATION", 422, {"reason": str(exc)}) from exc

        results.append(
            {
                "variant_id": treatment.id,
                "variant_code": treatment.code,
                "variant_name": treatment.name,
                "method": method,
                "metrics_count": outcome["metrics_count"],
                "significant_count": outcome["significant_count"],
                "primary": outcome["primary"],
                "items": outcome["items"],
                "note": outcome["note"],
            }
        )
    return {
        "experiment_id": experiment.id,
        "experiment_code": experiment.code,
        "method": method,
        "method_label": CORRECTION_LABELS.get(method, method),
        "results": results,
        "total": sum(len(item["items"]) for item in results),
        "interval_segments": segments,
    }


def apply_to_version(db: Session, version: AnalysisVersion, outcome: dict, *, operator_email: str) -> None:
    """把计算结果写进版本：同版本同 (变体, 指标) 就地覆盖，不留半套结果。"""
    for treatment in outcome["results"]:
        for item in treatment["items"]:
            row = db.scalar(
                select(MetricResult).where(
                    MetricResult.analysis_version_id == version.id,
                    MetricResult.variant_code == treatment["variant_code"],
                    MetricResult.metric_key == item["metric_key"],
                )
            )
            if row is None:
                row = MetricResult(
                    experiment_id=version.experiment_id,
                    analysis_version_id=version.id,
                    variant_id=treatment["variant_id"],
                    variant_code=treatment["variant_code"],
                    variant_name=treatment["variant_name"],
                    metric_key=item["metric_key"],
                    metric_name=item["metric_name"],
                )
                db.add(row)
            row.variant_id = treatment["variant_id"]
            row.variant_name = treatment["variant_name"]
            row.metric_name = item["metric_name"]
            row.is_primary = item["is_primary"]
            row.method = outcome["method"]
            row.control_n = item["control_n"]
            row.control_conversions = item["control_conversions"]
            row.variant_n = item["variant_n"]
            row.variant_conversions = item["variant_conversions"]
            row.control_rate = item["control_rate"]
            row.variant_rate = item["variant_rate"]
            row.diff = item["diff"]
            row.z = item["z"]
            row.p_value = item["p_value"]
            row.q_value = item["q_value"]
            row.q_bh = item["q_bh"]
            row.q_bonferroni = item["q_bonferroni"]
            row.ci_low = item["ci_low"]
            row.ci_high = item["ci_high"]
            row.lift = item["lift"]
            row.lift_ci_low = item["lift_ci_low"]
            row.lift_ci_high = item["lift_ci_high"]
            row.significant = item["significant"]
            row.verdict = item["verdict"]
            row.required_n = item["required_n"]
            row.power_at_current = item["power_at_current"]
            row.alpha = item["alpha"]
            row.payload = item
            row.computed_at = utcnow()
            row.computed_by = operator_email
            db.flush()
    # 清理本版本里本次未覆盖的旧 (变体, 指标)：只动本次重算的变体，
    # 单变体重跑不会误删其它变体的整套结果。
    touched = {item["variant_code"] for item in outcome["results"]}
    kept = {(item["variant_code"], item["metric_key"]) for item in outcome["results"] for item in item["items"]}
    stale = db.scalars(select(MetricResult).where(MetricResult.analysis_version_id == version.id)).all()
    for row in stale:
        if row.variant_code in touched and (row.variant_code, row.metric_key) not in kept:
            db.delete(row)
    db.flush()


def version_metrics(db: Session, version: AnalysisVersion) -> list[MetricResult]:
    """某版本的整套指标结果（含已失效版本，供追溯）。"""
    return list(
        db.scalars(
            select(MetricResult)
            .where(MetricResult.analysis_version_id == version.id)
            .order_by(MetricResult.variant_code, MetricResult.q_value, MetricResult.metric_key)
        ).all()
    )


def list_metrics(
    db: Session,
    *,
    experiment_id: int | None = None,
    variant_code: str | None = None,
    significant: bool | None = None,
    q: str | None = None,
    analysis_version_id: int | None = None,
    active_only: bool = True,
    limit: int = 40,
    offset: int = 0,
) -> dict:
    stmt = select(MetricResult)
    count_stmt = select(func.count()).select_from(MetricResult)
    if analysis_version_id is not None:
        stmt = stmt.where(MetricResult.analysis_version_id == analysis_version_id)
        count_stmt = count_stmt.where(MetricResult.analysis_version_id == analysis_version_id)
    elif active_only:
        active_ids = select(Experiment.active_analysis_version_id).where(
            Experiment.active_analysis_version_id.is_not(None)
        )
        stmt = stmt.where(MetricResult.analysis_version_id.in_(active_ids))
        count_stmt = count_stmt.where(MetricResult.analysis_version_id.in_(active_ids))
    if experiment_id:
        stmt = stmt.where(MetricResult.experiment_id == experiment_id)
        count_stmt = count_stmt.where(MetricResult.experiment_id == experiment_id)
    if variant_code:
        stmt = stmt.where(MetricResult.variant_code == variant_code)
        count_stmt = count_stmt.where(MetricResult.variant_code == variant_code)
    if significant is not None:
        stmt = stmt.where(MetricResult.significant.is_(significant))
        count_stmt = count_stmt.where(MetricResult.significant.is_(significant))
    if q:
        like = f"%{q.strip()}%"
        clause = or_(MetricResult.metric_name.ilike(like), MetricResult.variant_code.ilike(like))
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(MetricResult.experiment_id, MetricResult.variant_code, MetricResult.q_value, MetricResult.metric_key)
        .limit(limit)
        .offset(offset)
    ).all()
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
    items = []
    for row in rows:
        payload = serialize(row, version=versions.get(row.analysis_version_id))
        experiment = experiments.get(row.experiment_id)
        payload["experiment_code"] = experiment.code if experiment else ""
        payload["experiment_name"] = experiment.name if experiment else ""
        items.append(payload)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def significant_rows(db: Session, limit: int = 5) -> list[dict]:
    """总览「校正后显著」只看当前生效版本，失效版本的结果不会露出来。"""
    active_ids = select(Experiment.active_analysis_version_id).where(Experiment.active_analysis_version_id.is_not(None))
    rows = db.scalars(
        select(MetricResult)
        .where(MetricResult.significant.is_(True), MetricResult.analysis_version_id.in_(active_ids))
        .order_by(MetricResult.q_value, MetricResult.lift.desc())
        .limit(limit)
    ).all()
    experiment_codes = {
        item.id: (item.code, item.name)
        for item in db.scalars(select(Experiment).where(Experiment.id.in_([row.experiment_id for row in rows]))).all()
    } if rows else {}
    return [
        {
            **serialize(row),
            "experiment_code": experiment_codes.get(row.experiment_id, ("", ""))[0],
            "experiment_name": experiment_codes.get(row.experiment_id, ("", ""))[1],
        }
        for row in rows
    ]
