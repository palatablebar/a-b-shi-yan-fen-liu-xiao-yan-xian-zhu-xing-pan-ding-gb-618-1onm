"""导出服务：实验台账 / 分流样本 / 指标检验 / 决策清单 CSV。

所有导出都能追溯分析版本：
- 默认导出「当前生效版本」的整套结果（失效版本不会冒充有效结论）；
- 传 analysis_version_id 可导出任意历史版本（含已失效），由 version_status 列标明；
- assignments 按所选版本区间过滤；每行带「区间内」列，标明是否落在区间里。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CHANNEL_LABELS,
    CORRECTION_LABELS,
    EXPERIMENT_STATUS_LABELS,
    METRIC_LABELS,
    SRM_STATUS_LABELS,
    VERDICT_LABELS,
)
from src.models.analysis_version import AnalysisVersion
from src.models.assignment import Assignment
from src.models.decision import Decision
from src.models.experiment import Experiment
from src.models.metric_result import MetricResult
from src.models.variant import Variant
from src.services import analysis_service
from src.services.algorithms import exposure
from src.utils.csv_export import to_csv


def _fmt(value: float | None, digits: int = 4) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def _selected_version(db: Session, experiment: Experiment | None, analysis_version_id: int | None) -> AnalysisVersion | None:
    if analysis_version_id is not None and experiment is not None:
        return analysis_service.get_version(db, experiment, analysis_version_id)
    if experiment is not None:
        return analysis_service.active_version(db, experiment)
    return None


def experiments_csv(
    db: Session,
    experiment_id: int | None = None,
    analysis_version_id: int | None = None,
) -> bytes:
    stmt = select(Experiment)
    if experiment_id:
        stmt = stmt.where(Experiment.id == experiment_id)
    experiments = db.scalars(stmt.order_by(Experiment.id)).all()
    variants = {row.id: row for row in db.scalars(select(Variant)).all()}
    # 台账里的「决策」只取当前生效版本上的决策；历史决策在 decisions.csv 里带状态
    decisions = {}
    for row in db.scalars(select(Decision)).all():
        if row.experiment_id not in decisions or row.id > decisions[row.experiment_id].id:
            decisions[row.experiment_id] = row
    for item in experiments:
        active_decision = next(
            (
                row
                for row in db.scalars(
                    select(Decision).where(
                        Decision.experiment_id == item.id,
                        Decision.analysis_version_id == item.active_analysis_version_id,
                    )
                ).all()
            ),
            None,
        )
        if active_decision is not None:
            decisions[item.id] = active_decision
        else:
            decisions.pop(item.id, None)
    versions = {
        row.id: row
        for row in db.scalars(
            select(AnalysisVersion).where(
                AnalysisVersion.id.in_([item.active_analysis_version_id for item in experiments if item.active_analysis_version_id])
            )
        ).all()
    } if experiments else {}
    headers = [
        "实验编号", "实验名称", "主指标", "状态", "负责人", "基准转化率", "目标最小效应",
        "分析版本 v", "版本状态", "可用曝光区间", "区间天数",
        "SRM χ²", "df", "SRM p 值", "SRM 状态", "每变体所需样本", "当前功效", "当前可检测效应",
        "变体分流计划", "决策", "决策版本 v", "开始日期", "结束日期",
    ]
    rows = []
    for item in experiments:
        plan = " / ".join(
            f"{variant.code}:{variant.ratio:.2f}"
            for variant in sorted(variants.values(), key=lambda v: (v.experiment_id, v.sort_order))
            if variant.experiment_id == item.id
        )
        decision = decisions.get(item.id)
        version = versions.get(item.active_analysis_version_id)
        rows.append(
            [
                item.code,
                item.name,
                METRIC_LABELS.get(item.primary_metric, item.primary_metric),
                EXPERIMENT_STATUS_LABELS.get(item.status, item.status),
                item.owner,
                _fmt(item.baseline_rate),
                _fmt(item.planned_mde),
                f"v{item.active_analysis_revision}" if item.active_analysis_revision else "",
                analysis_service.STATUS_LABELS.get(version.status, "") if version else "",
                exposure.interval_label(item.active_interval_segments),
                item.active_interval_days or "",
                _fmt(item.srm_chi2, 2),
                item.srm_df or "",
                _fmt(item.srm_p_value, 6),
                SRM_STATUS_LABELS.get(item.srm_status, item.srm_status),
                item.required_per_variant or "",
                _fmt(item.power_at_current),
                _fmt(item.mde_at_current),
                plan,
                VERDICT_LABELS.get(decision.verdict, "") if decision else "",
                f"v{decision.analysis_version_revision}" if decision else "",
                item.started_on.isoformat() if item.started_on else "",
                item.ended_on.isoformat() if item.ended_on else "",
            ]
        )
    return to_csv(headers, rows)


def assignments_csv(
    db: Session,
    experiment_id: int | None = None,
    analysis_version_id: int | None = None,
) -> bytes:
    experiment = db.get(Experiment, experiment_id) if experiment_id else None
    version = _selected_version(db, experiment, analysis_version_id)
    segments = version.segments if version else None

    stmt = select(Assignment)
    if experiment_id:
        stmt = stmt.where(Assignment.experiment_id == experiment_id)
    interval = analysis_service.assignment_date_filter(segments)
    if interval is not None:
        stmt = stmt.where(interval)
    rows_in = db.scalars(stmt.order_by(Assignment.experiment_id, Assignment.stat_date, Assignment.channel)).all()
    variants = {row.id: row for row in db.scalars(select(Variant)).all()}
    experiments = {row.id: row for row in db.scalars(select(Experiment)).all()}
    headers = [
        "实验编号", "变体", "渠道", "指标", "日期", "样本数", "转化数", "转化率", "记录人",
        "分析版本 v", "可用曝光区间", "区间内",
    ]
    version_cache: dict[int, AnalysisVersion | None] = {}
    rows = []
    for row in rows_in:
        item = experiments.get(row.experiment_id)
        if row.experiment_id not in version_cache:
            version_cache[row.experiment_id] = version if (version and experiment_id == row.experiment_id) else (
                _selected_version(db, item, None) if item else None
            )
        item_version = version_cache[row.experiment_id]
        rows.append(
            [
                item.code if item else "",
                variants.get(row.variant_id).code if variants.get(row.variant_id) else "",
                CHANNEL_LABELS.get(row.channel, row.channel),
                METRIC_LABELS.get(row.metric_key, row.metric_key),
                row.stat_date.isoformat() if row.stat_date else "",
                row.samples,
                row.conversions,
                _fmt(row.conversions / row.samples if row.samples else 0.0),
                row.operator_email,
                f"v{item_version.revision}" if item_version else "",
                exposure.interval_label(item_version.segments) if item_version else "",
                "是" if item_version and exposure.contains(item_version.segments, row.stat_date) else "否",
            ]
        )
    return to_csv(headers, rows)


def metrics_csv(
    db: Session,
    experiment_id: int | None = None,
    analysis_version_id: int | None = None,
) -> bytes:
    experiment = db.get(Experiment, experiment_id) if experiment_id else None
    version = _selected_version(db, experiment, analysis_version_id)

    stmt = select(MetricResult)
    if experiment_id:
        stmt = stmt.where(MetricResult.experiment_id == experiment_id)
    if version is not None:
        stmt = stmt.where(MetricResult.analysis_version_id == version.id)
    else:
        active_ids = select(Experiment.active_analysis_version_id).where(
            Experiment.active_analysis_version_id.is_not(None)
        )
        stmt = stmt.where(MetricResult.analysis_version_id.in_(active_ids))
    rows_in = db.scalars(
        stmt.order_by(MetricResult.experiment_id, MetricResult.variant_code, MetricResult.q_value)
    ).all()
    experiments = {row.id: row for row in db.scalars(select(Experiment)).all()}
    versions = {
        row.id: row
        for row in db.scalars(
            select(AnalysisVersion).where(AnalysisVersion.id.in_([item.analysis_version_id for item in rows_in]))
        ).all()
    } if rows_in else {}
    headers = [
        "实验编号", "分析版本 v", "版本状态", "可用曝光区间", "变体", "指标", "是否主指标", "校正方法",
        "对照样本", "对照转化", "对照转化率", "实验样本", "实验转化", "实验转化率", "绝对差",
        "z 值", "p 值", "q 值(BH)", "q 值(Bonferroni)", "CI 下限", "CI 上限",
        "相对提升", "lift CI 下限", "lift CI 上限", "显著", "结论", "所需样本", "当前功效",
    ]
    rows = []
    for item in rows_in:
        experiment = experiments.get(item.experiment_id)
        item_version = versions.get(item.analysis_version_id)
        rows.append(
            [
                experiment.code if experiment else "",
                f"v{item_version.revision}" if item_version else "",
                analysis_service.STATUS_LABELS.get(item_version.status, "") if item_version else "",
                exposure.interval_label(item_version.segments) if item_version else "",
                item.variant_code,
                item.metric_name or METRIC_LABELS.get(item.metric_key, item.metric_key),
                "是" if item.is_primary else "否",
                CORRECTION_LABELS.get(item.method, item.method),
                item.control_n,
                item.control_conversions,
                _fmt(item.control_rate),
                item.variant_n,
                item.variant_conversions,
                _fmt(item.variant_rate),
                _fmt(item.diff),
                _fmt(item.z, 3),
                _fmt(item.p_value, 6),
                _fmt(item.q_bh, 6),
                _fmt(item.q_bonferroni, 6),
                _fmt(item.ci_low),
                _fmt(item.ci_high),
                _fmt(item.lift),
                _fmt(item.lift_ci_low),
                _fmt(item.lift_ci_high),
                "是" if item.significant else "否",
                VERDICT_LABELS.get(item.verdict, item.verdict),
                item.required_n,
                _fmt(item.power_at_current),
            ]
        )
    return to_csv(headers, rows)


def decisions_csv(
    db: Session,
    experiment_id: int | None = None,
    analysis_version_id: int | None = None,
) -> bytes:
    stmt = select(Decision)
    if experiment_id:
        stmt = stmt.where(Decision.experiment_id == experiment_id)
    if analysis_version_id is not None:
        stmt = stmt.where(Decision.analysis_version_id == analysis_version_id)
    decisions = db.scalars(stmt.order_by(Decision.id)).all()
    experiments = {
        row.id: row for row in db.scalars(select(Experiment).where(Experiment.id.in_([item.experiment_id for item in decisions]))).all()
    } if decisions else {}
    versions = {
        row.id: row
        for row in db.scalars(
            select(AnalysisVersion).where(AnalysisVersion.id.in_([item.analysis_version_id for item in decisions]))
        ).all()
    } if decisions else {}
    active_version_ids = {row.id: row.active_analysis_version_id for row in experiments.values()}
    headers = [
        "实验编号", "实验名称", "结论", "理由", "决策人", "决策时间",
        "分析版本 v", "版本状态", "可用曝光区间", "决策是否有效", "SRM p 值（决策版本）",
    ]
    rows = []
    for item in decisions:
        experiment = experiments.get(item.experiment_id)
        version = versions.get(item.analysis_version_id)
        valid = bool(
            experiment
            and version is not None
            and version.status == "active"
            and active_version_ids.get(experiment.id) == version.id
            and version.publication_count == item.analysis_version_publication
        )
        rows.append(
            [
                experiment.code if experiment else "",
                experiment.name if experiment else "",
                VERDICT_LABELS.get(item.verdict, item.verdict),
                item.rationale,
                item.decided_by,
                item.decided_at.strftime("%Y-%m-%d %H:%M") if item.decided_at else "",
                f"v{item.analysis_version_revision}",
                analysis_service.STATUS_LABELS.get(version.status, "") if version else "已失效",
                exposure.interval_label(version.segments) if version else "",
                "有效" if valid else "已失效（需重新决策）",
                _fmt(version.srm_p_value, 6) if version else "",
            ]
        )
    return to_csv(headers, rows)
