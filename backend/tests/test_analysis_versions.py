"""分析版本端到端：SQLite 内存库 + 真实 service 层。

覆盖核心不变量：
1. 同一组区间重跑不新增版本，结果整套一起发布；
2. 区间调整后旧版本立即失效，决策拒绝引用旧版本；
3. 区间只统计区间内样本（故障期不被累计进来）；
4. 发布失败沿用上一版 active，实验快照不变；
5. 决策 / 导出都能追到版本。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models import Base
from src.models.metric_result import MetricResult
from src.services import (
    analysis_service,
    assignment_service,
    decision_service,
    export_service,
    experiment_service,
    metric_service,
    srm_service,
)
from src.utils.errors import AppError


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _seed_experiment(db, *, days=7, include_gap_day=False):
    experiment = experiment_service.create_experiment(
        db,
        {
            "code": "EXP-TEST-ANALYSIS",
            "name": "分析版本测试",
            "primary_metric": "order_rate",
            "baseline_rate": 0.101,
            "planned_mde": 0.011,
            "alpha": 0.05,
            "power_target": 0.8,
            "started_on": date(2026, 6, 1),
            "variants": [
                {"code": "control", "name": "对照", "ratio": 0.5, "is_control": True},
                {"code": "treat", "name": "实验", "ratio": 0.5, "is_control": False},
            ],
        },
        owner_default="测试",
    )
    variants = {item.code: item for item in experiment_service.get_variants(db, experiment.id)}
    for offset in range(days):
        day = date(2026, 6, 1 + offset)
        for code, n, rate in (("control", 1000, 0.10), ("treat", 1000, 0.11)):
            assignment_service.upsert_assignment(
                db,
                experiment,
                variant_id=variants[code].id,
                channel="feed",
                metric_key="order_rate",
                stat_date=day,
                samples=n,
                conversions=int(n * rate),
                operator_email="seed@test",
            )
    if include_gap_day:
        # 故障期：06-10 分流 SDK 重试把样本都灌给了对照组，如果被累计会明显扭曲
        assignment_service.upsert_assignment(
            db,
            experiment,
            variant_id=variants["control"].id,
            channel="feed",
            metric_key="order_rate",
            stat_date=date(2026, 6, 10),
            samples=5000,
            conversions=0,
            operator_email="seed@test",
        )
    db.flush()
    return experiment


def _publish(db, experiment, segments, **kwargs):
    version, created = analysis_service.register(db, experiment, segments_raw=segments, **kwargs)
    result = analysis_service.run_and_publish(
        db, experiment, version=version, operator_email="admin@test"
    )
    return version, created, result


def test_same_interval_rerun_reuses_version(db):
    experiment = _seed_experiment(db)
    segments = [{"start": "2026-06-01", "end": "2026-06-07"}]
    version1, created1, _ = _publish(db, experiment, segments)
    version2, created2, _ = _publish(db, experiment, segments)
    assert created1 is True and created2 is False
    assert version1.id == version2.id
    assert analysis_service.list_versions(db, experiment.id)["total"] == 1
    assert experiment.active_analysis_version_id == version1.id
    assert analysis_service.active_version(db, experiment).status == "active"


def test_interval_change_invalidates_old_version_and_decision(db):
    experiment = _seed_experiment(db)
    v1, _, _ = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    decision = decision_service.create(
        db,
        experiment,
        {"verdict": "ship", "rationale": "初版区间结论：上线"},
        decided_by="admin@test",
    )
    assert decision.analysis_version_id == v1.id

    v2, created, _ = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-05"}])
    assert created is True and v2.id != v1.id
    assert v1.status == "superseded"
    assert v2.status == "active"
    assert experiment.active_analysis_version_id == v2.id

    # 旧决策仍在库，但不再是「当前有效决策」
    old = db.get(type(decision), decision.id)
    assert old is not None
    assert decision_service.get_by_experiment(db, experiment.id) is None
    valid, reason = analysis_service.ensure_decision_version_active(db, experiment, old)
    assert valid is False and "已失效" in reason

    # 可以对新的生效版本重新出决策；旧决策保留为历史
    new_decision = decision_service.create(
        db,
        experiment,
        {"verdict": "continue", "rationale": "按可用曝光区间重算后继续观察"},
        decided_by="admin@test",
    )
    assert new_decision.analysis_version_id == v2.id
    history = decision_service.history_by_experiment(db, experiment.id)
    assert {row.analysis_version_id for row in history} == {v1.id, v2.id}

    # 同一版本重复出决策 → 409
    with pytest.raises(AppError) as exc:
        decision_service.create(
            db, experiment, {"verdict": "ship", "rationale": "重复提交"}, decided_by="admin@test"
        )
    assert exc.value.status_code == 409


def test_decision_rejected_when_explicit_stale_version(db):
    experiment = _seed_experiment(db)
    v1, _, _ = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-05"}])
    from src.controllers import decision_controller

    with pytest.raises(AppError) as exc:
        decision_controller.create_decision(
            db,
            experiment.id,
            {
                "verdict": "ship",
                "rationale": "拿旧版本结论上线",
                "analysis_version_id": v1.id,
            },
            {"email": "admin@test", "role": "admin"},
            None,
        )
    assert exc.value.status_code == 409
    assert "失效" in exc.value.detail["reason"]


def test_gap_day_excluded_from_analysis(db):
    experiment = _seed_experiment(db, days=9, include_gap_day=True)
    segments = [
        {"start": "2026-06-01", "end": "2026-06-09"},
        {"start": "2026-06-10", "end": "2026-06-10"},
    ]
    # 全区间：故障期 5000×2 无效样本会被累计，分流占比严重失衡
    all_version, _, all_result = _publish(db, experiment, segments)
    assert all_result["version"]["srm"]["payload"]["total"] == 9 * 2000 + 5000
    assert all_result["srm"]["alarm"] is True
    db.commit()

    # 避开故障期（06-10）：只用 06-01~06-09
    clean_segments = [{"start": "2026-06-01", "end": "2026-06-09"}]
    clean_version, _, clean_result = _publish(db, experiment, clean_segments, note="跳过故障期")
    assert clean_result["version"]["srm"]["payload"]["total"] == 9 * 2000
    assert all_version.status == "superseded"
    assert clean_version.status == "active"
    # 区间外行数统计
    assert analysis_service.out_of_interval_rows(db, experiment, clean_segments) == 1


def test_publish_failure_keeps_previous_active(db):
    experiment = _seed_experiment(db)
    v1, _, result1 = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    assert v1.status == "active"

    # 区间内没有任何样本 → NO_EXPOSURE → 失败不发布
    empty_segments = [{"start": "2026-05-01", "end": "2026-05-03"}]
    version2, created2 = analysis_service.register(db, experiment, segments_raw=empty_segments, note="错的区间")
    db.commit()
    with pytest.raises(AppError) as exc:
        analysis_service.run_and_publish(db, experiment, version=version2, operator_email="admin@test")
    assert exc.value.status_code == 422
    db.refresh(experiment)
    failed = analysis_service.get_version(db, experiment, version2.id)
    analysis_service.mark_failure(db, failed, exc.value.detail["reason"])
    db.commit()
    assert failed.status == "draft"
    assert failed.failure_reason
    # 实验仍沿用上一版 active 快照
    assert experiment.active_analysis_version_id == v1.id
    assert analysis_service.active_version(db, experiment).id == v1.id
    assert srm_service.snapshot(experiment)["chi2"] == result1["srm"]["chi2"]


def test_deprecated_endpoints_rerun_within_active_interval(db):
    from src.controllers import metric_controller

    experiment = _seed_experiment(db)
    v1, _, _ = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-05"}])
    # 兼容入口按生效版本区间重跑，不新增版本、不按整段累计
    result = metric_controller.rerun_active_version(
        db, experiment.id, {"email": "admin@test", "role": "admin"}, None, piece="metrics", payload={}
    )
    assert result["total"] == 1
    assert analysis_service.list_versions(db, experiment.id)["total"] == 1
    assert experiment.active_analysis_version_id == v1.id
    assert analysis_service.active_version(db, experiment).status == "active"
    snapshot = srm_service.snapshot(experiment)
    assert snapshot["analysis_version_id"] == v1.id
    # 区间内样本 = 5 天 × 2 变体 × 1000
    assert snapshot["payload"]["total"] == 5 * 2000


def test_single_variant_rerun_keeps_other_variant_results(db):
    experiment = _seed_experiment(db)
    variants = {item.code: item for item in experiment_service.get_variants(db, experiment.id)}
    # 第二个实验组，确保单变体重跑不会删掉另一个变体的结果
    treat2 = type(variants["treat"])(
        experiment_id=experiment.id, code="treat2", name="实验 2", ratio=0.0, is_control=False, sort_order=2
    )
    db.add(treat2)
    db.flush()
    for offset in range(7):
        assignment_service.upsert_assignment(
            db,
            experiment,
            variant_id=treat2.id,
            channel="feed",
            metric_key="order_rate",
            stat_date=date(2026, 6, 1 + offset),
            samples=1000,
            conversions=120,
            operator_email="seed@test",
        )
    v1, _, outcome = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    db.commit()
    rows_before = db.query(MetricResult).filter(MetricResult.analysis_version_id == v1.id).count()
    analysis_service.run_and_publish(
        db, experiment, version=v1, operator_email="admin@test", variant_code="treat"
    )
    db.commit()
    rows_after = db.query(MetricResult).filter(MetricResult.analysis_version_id == v1.id).count()
    assert rows_after == rows_before
    # 不影响实验上的生效版本
    assert experiment.active_analysis_version_id == v1.id


def test_failure_midway_writes_nothing_and_keeps_active(db, monkeypatch):
    experiment = _seed_experiment(db)
    v1, _, result1 = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    db.commit()
    before_metrics = db.query(MetricResult).filter(MetricResult.analysis_version_id == v1.id).count()

    from src.services import metric_service

    def boom(*args, **kwargs):
        raise AppError("INTERNAL", 500, {"reason": "模拟发布中途失败"})

    monkeypatch.setattr(metric_service, "calculate", boom)
    v2, _, = analysis_service.register(
        db, experiment, segments_raw=[{"start": "2026-06-01", "end": "2026-06-05"}]
    )
    db.commit()
    with pytest.raises(AppError):
        analysis_service.run_and_publish(db, experiment, version=v2, operator_email="admin@test")
    db.refresh(experiment)
    db.refresh(v2)
    # 整套发布：失败后 v2 仍是 draft，没有任何指标结果；实验继续用 v1
    assert v2.status == "draft"
    assert db.query(MetricResult).filter(MetricResult.analysis_version_id == v2.id).count() == 0
    assert db.query(MetricResult).filter(MetricResult.analysis_version_id == v1.id).count() == before_metrics
    assert experiment.active_analysis_version_id == v1.id
    assert v1.status == "active"
    assert srm_service.snapshot(experiment)["chi2"] == result1["srm"]["chi2"]


def test_failure_on_active_version_keeps_published_at(db, monkeypatch):
    experiment = _seed_experiment(db)
    v1, _, _ = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    db.commit()
    published_at = v1.published_at
    from src.services import srm_service

    def boom(*args, **kwargs):
        raise AppError("NO_EXPOSURE", 422, {"reason": "模拟区间内无样本"})

    monkeypatch.setattr(srm_service, "calculate", boom)
    with pytest.raises(AppError):
        analysis_service.run_and_publish(db, experiment, version=v1, operator_email="admin@test")
    db.refresh(v1)
    db.refresh(experiment)
    analysis_service.mark_failure(db, v1, "模拟区间内无样本")
    db.commit()
    assert v1.status == "active"
    assert v1.published_at == published_at
    assert v1.failure_reason
    assert experiment.active_analysis_version_id == v1.id


def test_rerun_same_interval_after_decision_makes_decision_stale(db):
    experiment = _seed_experiment(db)
    v1, _, _ = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    decision = decision_service.create(
        db, experiment, {"verdict": "ship", "rationale": "初次决策"}, decided_by="admin@test"
    )
    db.commit()
    assert decision_service.serialize(decision, experiment, v1)["stale"] is False

    # 同区间补录样本后重跑：不新增版本，但发布轮次前进，旧决策失效
    assignment_service.upsert_assignment(
        db,
        experiment,
        variant_id=experiment_service.get_variants(db, experiment.id)[0].id,
        channel="feed",
        metric_key="order_rate",
        stat_date=date(2026, 6, 4),
        samples=100,
        conversions=30,
        operator_email="seed@test",
    )
    db.flush()
    _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    db.refresh(v1)
    assert v1.publication_count == 2
    assert analysis_service.list_versions(db, experiment.id)["total"] == 1
    assert decision_service.get_by_experiment(db, experiment.id) is None
    history = decision_service.history_by_experiment(db, experiment.id)
    assert len(history) == 1
    assert decision_service.serialize(history[0], experiment, v1)["stale"] is True
    valid, _ = analysis_service.ensure_decision_version_active(db, experiment, history[0])
    assert valid is False
    # 可以在同一版本的当前发布上重新出决策
    fresh = decision_service.create(
        db, experiment, {"verdict": "continue", "rationale": "重跑后结论改变"}, decided_by="admin@test"
    )
    assert fresh.analysis_version_id == v1.id
    assert fresh.analysis_version_publication == 2


def test_metrics_and_exports_trace_version(db):
    experiment = _seed_experiment(db)
    v1, _, _ = _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-07"}])
    # 所有指标结果都绑定 v1
    rows = db.query(MetricResult).all()
    assert rows and all(row.analysis_version_id == v1.id for row in rows)
    db.commit()

    metrics_csv = export_service.metrics_csv(db, experiment.id, v1.id)
    text = metrics_csv.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert "分析版本 v" in header and "可用曝光区间" in header
    assert "v1" in text

    decision = decision_service.create(
        db,
        experiment,
        {"verdict": "ship", "rationale": "区间内主指标显著为正"},
        decided_by="admin@test",
    )
    decisions_csv = export_service.decisions_csv(db)
    decision_text = decisions_csv.decode("utf-8-sig")
    assert "分析版本 v" in decision_text.splitlines()[0]
    assert "决策是否有效" in decision_text.splitlines()[0]
    assert "有效" in decision_text and "v1" in decision_text
    # 区间调整后，同一决策在导出里标为已失效
    _publish(db, experiment, [{"start": "2026-06-01", "end": "2026-06-05"}])
    stale_text = export_service.decisions_csv(db).decode("utf-8-sig")
    assert "已失效（需重新决策）" in stale_text
    assert decision.analysis_version_id == v1.id

    # 调整区间后，导出的 metrics 默认只含新版本
    v2 = next(
        item for item in analysis_service.list_versions(db, experiment.id)["items"] if item["revision"] == 2
    )
    v2_id = v2["id"]
    default_rows = metric_service.list_metrics(db, experiment_id=experiment.id)["items"]
    assert default_rows and all(row["analysis_version_id"] == v2_id for row in default_rows)
    # 显式按版本追溯旧结果仍可读
    old_rows = metric_service.list_metrics(db, experiment_id=experiment.id, analysis_version_id=v1.id)["items"]
    old_version = next(row for row in analysis_service.list_versions(db, experiment.id)["items"] if row["id"] == v1.id)
    assert old_version["status"] == "superseded"
    assert old_rows and all(row["analysis_version_id"] == v1.id for row in old_rows)
