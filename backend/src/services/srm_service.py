"""算法 A 的落库层：在指定可用曝光区间内聚合分流样本 → 跑 SRM/功效。

- `calculate(...)` 是纯计算（不写库），供分析版本发布用；
- `apply_to_version(...)` 把结果写进分析版本；
- `mirror_to_experiment(...)` 把当前生效版本的快照同步到实验，页面/导出只读实验；
- 不再提供「按整段累计重算」的旧入口，避免把故障期样本混进结论。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.models.analysis_version import AnalysisVersion
from src.models.experiment import Experiment
from src.services import analysis_service, assignment_service
from src.services.algorithms import exposure
from src.services.algorithms import srm as srm_algorithm
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, utcnow


def build_variants(db: Session, experiment: Experiment, segments: list[dict] | None) -> list[dict]:
    """区间内的变体主指标样本合计（SRM 输入）。"""
    totals = assignment_service.variant_totals(db, experiment, segments=segments)
    return [
        {
            "code": row["code"],
            "name": row["name"],
            "ratio": row["ratio"],
            "observed": row["samples"],
            "is_control": row["is_control"],
            "conversions": row["conversions"],
        }
        for row in totals
    ]


def calculate(
    db: Session,
    experiment: Experiment,
    segments: list[dict],
    *,
    alpha: float | None = None,
    power: float | None = None,
    alarm_p: float | None = None,
) -> dict:
    """在区间内跑 SRM；区间内无样本 → 422 NO_EXPOSURE，由调用方沿用上一版。"""
    variants = build_variants(db, experiment, segments)
    if sum(item["observed"] for item in variants) <= 0:
        if segments:
            analysis_service.guard_no_exposure(experiment, segments)
        raise AppError(
            "NO_SAMPLES",
            422,
            {"reason": "该实验还没有分流样本，先按变体/渠道/日期录入样本再校验", "experiment": experiment.code},
        )
    try:
        result = srm_algorithm.run_srm(
            variants,
            baseline_rate=experiment.baseline_rate,
            planned_mde=experiment.planned_mde,
            alpha=alpha if alpha is not None else experiment.alpha,
            power=power if power is not None else experiment.power_target,
            alarm_p=alarm_p if alarm_p is not None else 0.001,
        )
    except ValueError as exc:
        raise AppError("VALIDATION", 422, {"reason": str(exc)}) from exc
    result["segments"] = segments
    result["interval_label"] = exposure.interval_label(segments)
    result["interval_days"] = exposure.total_days(segments)
    return result


def apply_to_version(version: AnalysisVersion, result: dict) -> None:
    version.srm_chi2 = result["chi2"]
    version.srm_df = result["df"]
    version.srm_p_value = result["p_value"]
    version.srm_status = result["status"]
    version.srm_alarm = result["alarm"]
    version.srm_checked_at = utcnow()
    version.required_per_variant = result["required_per_variant"]
    version.power_at_current = result["power_at_current"]
    version.mde_at_current = result["mde_at_current"]
    version.srm_payload = result


def mirror_to_experiment(experiment: Experiment, result: dict) -> None:
    """只允许把「当前生效版本」的快照写到实验上。"""
    experiment.srm_chi2 = result["chi2"]
    experiment.srm_df = result["df"]
    experiment.srm_p_value = result["p_value"]
    experiment.srm_status = result["status"]
    experiment.has_srm_alarm = result["alarm"]
    experiment.srm_checked_at = utcnow()
    experiment.required_per_variant = result["required_per_variant"]
    experiment.power_at_current = result["power_at_current"]
    experiment.mde_at_current = result["mde_at_current"]
    experiment.srm_payload = result


def snapshot(experiment: Experiment) -> dict:
    """实验上的当前生效快照（只读）。"""
    payload = experiment.srm_payload or {}
    return {
        "experiment_id": experiment.id,
        "experiment_code": experiment.code,
        "status": experiment.srm_status,
        "alarm": experiment.has_srm_alarm,
        "chi2": experiment.srm_chi2,
        "df": experiment.srm_df,
        "p_value": experiment.srm_p_value,
        "checked_at": iso_utc(experiment.srm_checked_at),
        "required_per_variant": experiment.required_per_variant,
        "power_at_current": experiment.power_at_current,
        "mde_at_current": experiment.mde_at_current,
        "baseline_rate": experiment.baseline_rate,
        "planned_mde": experiment.planned_mde,
        "payload": payload,
        "cells": payload.get("cells", []),
        "gap_total": payload.get("gap_total"),
        "note": payload.get("note", ""),
        "analysis_version_id": experiment.active_analysis_version_id,
        "analysis_revision": experiment.active_analysis_revision,
        "analysis_publication": experiment.active_analysis_publication,
        "interval_segments": experiment.active_interval_segments or [],
        "interval_label": exposure.interval_label(experiment.active_interval_segments),
        "interval_days": experiment.active_interval_days,
        "published_at": iso_utc(experiment.active_analysis_published_at),
    }
