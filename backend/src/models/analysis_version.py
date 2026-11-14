"""分析版本：一次分析登记的「可用曝光区间」+ 整套结果（SRM / 全部指标 / 建议）。

业务约束（README「分析版本」一节同步）：
- 同一实验下，归一化后的区间集合唯一（interval_key）；
- 重跑同一组区间复用同一版本，不新增版本；
- 一个版本的分流校验、全部指标检验与建议在同一事务里整套发布；
- 发布成功后，实验的其它版本立即置为 superseded（失效），决策拒绝引用失效版本；
- 计算失败则回滚，实验继续沿用它上一版 active 结果。

状态：
  draft       已登记区间，尚未成功发布（页面不可用于决策）
  active      当前生效版本
  superseded  区间被调整后失效
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class AnalysisVersion(Base):
    __tablename__ = "analysis_versions"
    __table_args__ = (
        UniqueConstraint("experiment_id", "interval_key", name="uq_analysis_version_interval"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # 发布轮次：同版本每整套发布一次 +1。决策只对「做出结论时的那次发布」有效，
    # 同区间重跑后旧决策自动失效、允许在同一版本上重新出决策。
    publication_count: Mapped[int] = mapped_column(Integer, default=0)

    # ---- 可用曝光区间（归一化、升序、不重叠）----
    interval_key: Mapped[str] = mapped_column(String(400))
    segments: Mapped[list] = mapped_column(JSON, default=list)
    interval_days: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")

    # ---- 本次分析参数 ----
    method: Mapped[str] = mapped_column(String(16), default="bh")
    alpha: Mapped[float] = mapped_column(Float, default=0.05)
    power_target: Mapped[float] = mapped_column(Float, default=0.8)

    # ---- 算法 A 快照 ----
    srm_chi2: Mapped[float | None] = mapped_column(Float, nullable=True)
    srm_df: Mapped[int | None] = mapped_column(Integer, nullable=True)
    srm_p_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    srm_status: Mapped[str] = mapped_column(String(12), default="unknown")
    srm_alarm: Mapped[bool] = mapped_column(Boolean, default=False)
    srm_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    required_per_variant: Mapped[int | None] = mapped_column(Integer, nullable=True)
    power_at_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    mde_at_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    srm_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # ---- 算法 B 汇总 + 建议 ----
    metrics_count: Mapped[int] = mapped_column(Integer, default=0)
    significant_count: Mapped[int] = mapped_column(Integer, default=0)
    suggestion_verdict: Mapped[str] = mapped_column(String(16), default="continue")
    suggestion_rationale: Mapped[str] = mapped_column(Text, default="")
    suggested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[str] = mapped_column(String(160), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, default="")
