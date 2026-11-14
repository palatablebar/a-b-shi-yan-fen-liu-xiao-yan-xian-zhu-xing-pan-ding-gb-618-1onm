"""分析版本服务：登记可用曝光区间 → 整套计算（分流校验 + 全部指标 + 建议）→ 原子发布。

不变量（README「分析版本」一节同步）：
1. 同一实验的可用曝光区间归一化后唯一：同一组区间重跑 = 复用同一版本，不新增版本；
2. 一次发布是整套写入：SRM 快照 + 全部指标结果 + 建议 + 实验上的 active 指针在同一事务；
3. 发布成功后，实验其它版本立即 status=superseded，实验上的 active 快照同步切换；
4. 任何一步失败：不发布、不切版本，实验继续沿用上一版 active 结果（页面提示 failure_reason）；
5. 只有 active 版本能被决策引用；superseded 版本一律拒绝（409），也不会被导出当作有效结论。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.analysis_version import AnalysisVersion
from src.models.assignment import Assignment
from src.models.experiment import Experiment
from src.services.algorithms import exposure
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, utcnow

STATUS_LABELS = {
    "draft": "待发布",
    "active": "生效中",
    "superseded": "已失效",
}


# ------------------------------------------------------------------ 区间读写
def serialize_version(row: AnalysisVersion, *, experiment: Experiment | None = None) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "experiment_code": experiment.code if experiment else "",
        "experiment_name": experiment.name if experiment else "",
        "revision": row.revision,
        "publication": row.publication_count,
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "is_active": row.status == "active",
        "interval_key": row.interval_key,
        "segments": row.segments or [],
        "interval_label": exposure.interval_label(row.segments),
        "interval_days": row.interval_days,
        "note": row.note,
        "method": row.method,
        "alpha": row.alpha,
        "power_target": row.power_target,
        "srm": {
            "status": row.srm_status,
            "alarm": row.srm_alarm,
            "chi2": row.srm_chi2,
            "df": row.srm_df,
            "p_value": row.srm_p_value,
            "checked_at": iso_utc(row.srm_checked_at),
            "required_per_variant": row.required_per_variant,
            "power_at_current": row.power_at_current,
            "mde_at_current": row.mde_at_current,
            "payload": row.srm_payload or {},
        },
        "metrics_count": row.metrics_count,
        "significant_count": row.significant_count,
        "suggestion": {
            "verdict": row.suggestion_verdict,
            "rationale": row.suggestion_rationale,
            "suggested_at": iso_utc(row.suggested_at),
        },
        "created_by": row.created_by,
        "created_at": iso_utc(row.created_at),
        "published_at": iso_utc(row.published_at),
        "superseded_at": iso_utc(row.superseded_at),
        "failure_reason": row.failure_reason,
    }


def active_version(db: Session, experiment: Experiment) -> AnalysisVersion | None:
    if experiment.active_analysis_version_id is None:
        return None
    row = db.get(AnalysisVersion, experiment.active_analysis_version_id)
    if row is None or row.experiment_id != experiment.id or row.status != "active":
        return None
    return row


def require_active_version(db: Session, experiment: Experiment) -> AnalysisVersion:
    row = active_version(db, experiment)
    if row is None:
        raise AppError(
            "NO_ANALYSIS_VERSION",
            409,
            {
                "reason": "该实验还没有生效中的分析版本：先登记可用曝光区间并发布分析，再出决策",
                "experiment": experiment.code,
            },
        )
    return row


def get_version(db: Session, experiment: Experiment, version_id: int) -> AnalysisVersion:
    row = db.get(AnalysisVersion, version_id)
    if row is None or row.experiment_id != experiment.id:
        raise AppError("NOT_FOUND", 404, {"entity": "analysis_version", "id": version_id})
    return row


def list_versions(db: Session, experiment_id: int) -> dict:
    rows = db.scalars(
        select(AnalysisVersion)
        .where(AnalysisVersion.experiment_id == experiment_id)
        .order_by(AnalysisVersion.revision.desc(), AnalysisVersion.id.desc())
    ).all()
    experiment = db.get(Experiment, experiment_id)
    return {
        "items": [serialize_version(row, experiment=experiment) for row in rows],
        "total": len(rows),
        "active_version_id": experiment.active_analysis_version_id if experiment else None,
    }


# ------------------------------------------------------------------ 区间过滤
def assignment_date_filter(segments: list[dict] | None):
    """SQLAlchemy 条件：只保留落在可用曝光区间内的分流样本行。"""
    bounds = exposure.segment_bounds(segments)
    if not bounds:
        return None
    from sqlalchemy import and_, or_

    clauses = [
        and_(Assignment.stat_date >= start, Assignment.stat_date <= end) for start, end in bounds
    ]
    return or_(*clauses)


def guard_no_exposure(experiment: Experiment, segments: list[dict]) -> None:
    raise AppError(
        "NO_EXPOSURE",
        422,
        {
            "reason": "登记的可用曝光区间内没有可用的分流样本，先补录区间内的样本或调整区间",
            "experiment": experiment.code,
            "interval_label": exposure.interval_label(segments),
        },
    )


def out_of_interval_rows(db: Session, experiment: Experiment, segments: list[dict]) -> int:
    """区间外样本行数：页面提示「有多少行被排除」，避免整段累计。"""
    condition = assignment_date_filter(segments)
    total = db.scalar(
        select(func.count()).select_from(Assignment).where(Assignment.experiment_id == experiment.id)
    ) or 0
    inside = db.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(Assignment.experiment_id == experiment.id, condition)
    ) or 0
    return int(total - inside)


# ------------------------------------------------------------------ 登记区间
def register(
    db: Session,
    experiment: Experiment,
    *,
    segments_raw: object,
    note: str = "",
    method: str = "bh",
    alpha: float | None = None,
    power: float | None = None,
    created_by: str = "system",
) -> tuple[AnalysisVersion, bool]:
    """登记（或复用）一个分析版本。

    返回 (version, created)。同一组区间已存在时直接复用，不新增版本、不改 revision。
    """
    try:
        segments = exposure.normalize_segments(segments_raw)
    except ValueError as exc:
        raise AppError("VALIDATION", 422, {"reason": str(exc)}) from exc
    key = exposure.interval_key(segments)
    existing = db.scalar(
        select(AnalysisVersion).where(
            AnalysisVersion.experiment_id == experiment.id,
            AnalysisVersion.interval_key == key,
        )
    )
    if existing is not None:
        # 重跑同一组区间：不新增版本，但允许按本次请求更新分析参数（方法/α/功效）
        if method in ("bh", "bonferroni"):
            existing.method = method
        if alpha is not None:
            existing.alpha = alpha
        if power is not None:
            existing.power_target = power
        if note:
            existing.note = note
        db.flush()
        return existing, False
    revision = (
        db.scalar(
            select(func.max(AnalysisVersion.revision)).where(AnalysisVersion.experiment_id == experiment.id)
        )
        or 0
    ) + 1
    row = AnalysisVersion(
        experiment_id=experiment.id,
        revision=revision,
        status="draft",
        interval_key=key,
        segments=segments,
        interval_days=exposure.total_days(segments),
        note=note or "",
        method=method if method in ("bh", "bonferroni") else "bh",
        alpha=alpha if alpha is not None else experiment.alpha,
        power_target=power if power is not None else experiment.power_target,
        created_by=created_by,
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row, True


# ------------------------------------------------------------------ 发布 / 失效
def publish(db: Session, experiment: Experiment, version: AnalysisVersion) -> AnalysisVersion:
    """把一个刚算完的版本整套发布：切换 active 指针并让旧版本失效。"""
    now = utcnow()
    others = db.scalars(
        select(AnalysisVersion).where(
            AnalysisVersion.experiment_id == experiment.id,
            AnalysisVersion.id != version.id,
            AnalysisVersion.status == "active",
        )
    ).all()
    for other in others:
        other.status = "superseded"
        other.superseded_at = now

    version.status = "active"
    version.published_at = now
    version.superseded_at = None
    version.failure_reason = ""
    version.publication_count = (version.publication_count or 0) + 1
    # 新发布版本以本次参数为准；同区间重跑时 register() 已就地更新过这些字段

    experiment.active_analysis_version_id = version.id
    experiment.active_interval_key = version.interval_key
    experiment.active_interval_segments = version.segments or []
    experiment.active_interval_days = version.interval_days
    experiment.active_analysis_revision = version.revision
    experiment.active_analysis_publication = version.publication_count
    experiment.active_analysis_published_at = version.published_at
    db.flush()
    return version


def mark_failure(db: Session, version: AnalysisVersion, reason: str) -> None:
    """计算失败：只记原因，不改版本状态、发布时间与实验上的 active 指针。

    新建版本本来就是 draft；已生效 / 历史版本保持原状态并继续展示其原有结果 ——
    实验沿用上一版 active 快照，页面据 failure_reason 提示「最近一次重跑失败」。
    """
    version.failure_reason = reason[:2000]


def summarize_metrics(db: Session, version: AnalysisVersion) -> dict:
    """发布后绑定指标结果数量与建议，供列表页直接展示。"""
    from src.models.metric_result import MetricResult

    rows = db.scalars(select(MetricResult).where(MetricResult.analysis_version_id == version.id)).all()
    version.metrics_count = len(rows)
    version.significant_count = sum(1 for row in rows if row.significant)
    primary = next((row for row in rows if row.is_primary), None)
    # 建议与本次全部指标一起重算：没有主指标时回到「继续观察」，不残留上一轮结论
    version.suggestion_verdict = primary.verdict if primary is not None else "continue"
    verdict_labels = {"ship": "上线", "stop": "停止", "continue": "继续观察"}
    version.suggestion_rationale = (
        f"主指标 {primary.metric_name}：lift {primary.lift:+.2%}，q={primary.q_value:.4g}，"
        f"建议{verdict_labels.get(version.suggestion_verdict, version.suggestion_verdict)}；"
        f"版本 v{version.revision}（{exposure.interval_label(version.segments)}）"
        if primary is not None
        else "没有主指标检验结果，建议继续观察。"
    )
    version.suggested_at = utcnow()
    db.flush()
    return {"metrics_count": version.metrics_count, "significant_count": version.significant_count}


def version_decision(db: Session, version: AnalysisVersion):
    from src.models.decision import Decision

    return db.scalar(select(Decision).where(Decision.analysis_version_id == version.id))


def ensure_decision_version_active(
    db: Session,
    experiment: Experiment,
    decision,
) -> tuple[bool, str]:
    """决策引用的版本是否仍然有效：返回 (是否有效, 失效原因)。"""
    if decision is None:
        return True, ""
    version = db.get(AnalysisVersion, decision.analysis_version_id)
    if version is None:
        return False, "决策引用的分析版本已不存在"
    if (
        version.status != "active"
        or experiment.active_analysis_version_id != version.id
        or version.publication_count != decision.analysis_version_publication
    ):
        return False, (
            f"决策引用的分析版本 v{version.revision}（{exposure.interval_label(version.segments)}，"
            f"第 {decision.analysis_version_publication} 次发布）已失效，需要按当前生效版本重新出决策"
        )
    return True, ""


def version_is_current(db: Session, experiment: Experiment, version_id: int | None) -> bool:
    """决策前的统一校验：只能是实验当前生效版本。"""
    return (
        version_id is not None
        and experiment.active_analysis_version_id == version_id
        and (version := db.get(AnalysisVersion, version_id)) is not None
        and version.status == "active"
        and version.experiment_id == experiment.id
    )


def decision_is_stale(decision, version: AnalysisVersion | None) -> bool:
    """决策是否已失效：版本不存在/非 active/不是实验当前版本，或该版本被重新发布过。"""
    if decision is None:
        return False
    if version is None or version.status != "active":
        return True
    return version.publication_count != decision.analysis_version_publication


def day_bounds(segments: list[dict] | None) -> tuple[date | None, date | None]:
    bounds = exposure.segment_bounds(segments)
    if not bounds:
        return None, None
    return bounds[0][0], bounds[-1][1]


# ------------------------------------------------------------------ 整套发布
def run_and_publish(
    db: Session,
    experiment: Experiment,
    *,
    version: AnalysisVersion,
    operator_email: str,
    variant_code: str | None = None,
) -> dict:
    """在版本区间内整套重算并发布；任一步失败都不发布，实验沿用上一版。

    写入放在 SAVEPOINT 里：失败只回滚本次计算与写入，已登记的区间版本保留为
    draft（并记 failure_reason），实验上的 active 指针与快照完全不动。
    """
    from src.services import metric_service, srm_service

    savepoint = db.begin_nested()
    try:
        srm_result = srm_service.calculate(
            db,
            experiment,
            version.segments or [],
            alpha=version.alpha,
            power=version.power_target,
        )
        metric_outcome = metric_service.calculate(
            db,
            experiment,
            version.segments or [],
            method=version.method,
            alpha=version.alpha,
            power=version.power_target,
            variant_code=variant_code,
        )

        srm_service.apply_to_version(version, srm_result)
        # 整套发布前先清空本版本的旧指标结果，避免半新半旧；
        # 单变体重跑只覆盖该变体，其它变体结果保持原样。
        if variant_code is None:
            from src.models.metric_result import MetricResult

            for row in db.scalars(select(MetricResult).where(MetricResult.analysis_version_id == version.id)).all():
                db.delete(row)
            db.flush()
        metric_service.apply_to_version(db, version, metric_outcome, operator_email=operator_email)

        publish(db, experiment, version)
        summary = summarize_metrics(db, version)
        srm_service.mirror_to_experiment(experiment, srm_result)
        db.flush()
    except Exception:
        savepoint.rollback()
        raise
    savepoint.commit()
    return {
        "version": serialize_version(version, experiment=experiment),
        "srm": srm_service.snapshot(experiment),
        "metric_result": metric_outcome,
        "summary": summary,
    }
