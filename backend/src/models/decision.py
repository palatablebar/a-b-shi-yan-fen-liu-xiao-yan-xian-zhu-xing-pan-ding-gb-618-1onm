"""决策：一次实验一个有效结论（上线 / 继续观察 / 停止），绑定做出结论时的分析版本。

- 同一分析版本的同一次发布只能有一条决策（409）；
- 决策引用的版本一旦失效（区间被调整）或该版本被同区间重跑（发布轮次前进），
  决策不再有效，也不再被导出为有效结论；可对当前生效发布重新出决策，旧决策仅作历史留档。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Decision(Base):
    __tablename__ = "decisions"
    # 自然键 = (experiment_id, analysis_version_id, analysis_version_publication)：
    # 同一版本的「同一次发布」只能出一次决策；版本失效或同区间重跑后，
    # 旧决策保留为历史（导出标「已失效」），可对新的生效发布重新出结论。
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "analysis_version_id",
            "analysis_version_publication",
            name="uq_decision_experiment_version_publication",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    analysis_version_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="CASCADE"), index=True
    )
    # 决策落库时的版本指纹，导出与审计据此追溯；版本失效后仍保留
    analysis_version_revision: Mapped[int] = mapped_column(Integer, default=1)
    analysis_version_publication: Mapped[int] = mapped_column(Integer, default=0)
    interval_key: Mapped[str] = mapped_column(String(400), default="")
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(160), default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
