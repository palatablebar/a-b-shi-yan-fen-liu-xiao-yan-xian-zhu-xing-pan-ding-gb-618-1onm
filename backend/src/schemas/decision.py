from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DecisionIn(BaseModel):
    verdict: Literal["ship", "continue", "stop"]
    rationale: str = Field(min_length=4, max_length=2000)
    # 可选：显式声明决策所依据的分析版本；不填则默认当前生效版本。
    # 传了失效版本 / 非当前生效版本 → 409，不允许拿旧结论上线。
    analysis_version_id: int | None = None
