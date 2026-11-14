from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.constants.enums import Channel, ExperimentStatus, MetricKey

STATUS_VALUES = tuple(item.value for item in ExperimentStatus)
METRIC_VALUES = tuple(item.value for item in MetricKey)
CHANNEL_VALUES = tuple(item.value for item in Channel)


class VariantIn(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=2, max_length=120)
    ratio: float = Field(gt=0, le=1)
    is_control: bool = False
    note: str | None = Field(default=None, max_length=500)


class ExperimentIn(BaseModel):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str = Field(min_length=2, max_length=120)
    hypothesis: str = Field(default="", max_length=2000)
    owner: str | None = Field(default=None, max_length=80)
    primary_metric: str = Field(default="order_rate")
    status: str = "running"
    baseline_rate: float = Field(default=0.1, gt=0, lt=1)
    planned_mde: float = Field(default=0.01, gt=0, le=0.5)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    power_target: float = Field(default=0.8, gt=0, lt=1)
    traffic_note: str | None = Field(default=None, max_length=500)
    started_on: date | None = None
    ended_on: date | None = None
    variants: list[VariantIn] = Field(default_factory=list, min_length=2, max_length=4)

    @field_validator("primary_metric")
    @classmethod
    def _metric_ok(cls, value: str) -> str:
        if value not in METRIC_VALUES:
            raise ValueError(f"primary_metric 必须是 {METRIC_VALUES} 之一")
        return value

    @field_validator("status")
    @classmethod
    def _status_ok(cls, value: str) -> str:
        if value not in STATUS_VALUES:
            raise ValueError(f"status 必须是 {STATUS_VALUES} 之一")
        return value

    @field_validator("variants")
    @classmethod
    def _variants_ok(cls, value: list[VariantIn]) -> list[VariantIn]:
        codes = [item.code for item in value]
        if len(set(codes)) != len(codes):
            raise ValueError("变体编码不能重复")
        if sum(1 for item in value if item.is_control) != 1:
            raise ValueError("必须有且只有一个对照组")
        total = sum(item.ratio for item in value)
        if abs(total - 1.0) > 0.02:
            raise ValueError(f"计划分流比例合计应约等于 1，当前 {total:.3f}")
        return value


class ExperimentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    hypothesis: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=80)
    status: Literal["draft", "running", "paused", "finished", "archived"] | None = None
    baseline_rate: float | None = Field(default=None, gt=0, lt=1)
    planned_mde: float | None = Field(default=None, gt=0, le=0.5)
    alpha: float | None = Field(default=None, gt=0, lt=0.5)
    power_target: float | None = Field(default=None, gt=0, lt=1)
    traffic_note: str | None = Field(default=None, max_length=500)


class ExposureSegmentIn(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> "ExposureSegmentIn":
        if self.end < self.start:
            raise ValueError("区间结束日期不能早于开始日期")
        return self


class AnalysisRunIn(BaseModel):
    """登记一组可用曝光区间并整套重算发布。

    两种写法等价：
      {"segments": [{"start": "...", "end": "..."}, ...]}
      {"interval_start": "...", "interval_end": "..."}   # 单段简写
    """

    segments: list[ExposureSegmentIn] | None = Field(default=None, min_length=1, max_length=20)
    interval_start: date | None = None
    interval_end: date | None = None
    note: str = Field(default="", max_length=500)
    method: Literal["bh", "bonferroni"] = "bh"
    alpha: float | None = Field(default=None, gt=0, lt=0.5)
    power: float | None = Field(default=None, gt=0, lt=1)
    variant_code: str | None = Field(default=None, max_length=40)
    reuse_existing: bool = True

    @model_validator(mode="after")
    def _normalize(self) -> "AnalysisRunIn":
        if self.segments and (self.interval_start or self.interval_end):
            raise ValueError("segments 与 interval_start/interval_end 不能同时使用")
        if not self.segments and not (self.interval_start and self.interval_end):
            raise ValueError("必须登记至少一段可用曝光区间（segments 或 interval_start/interval_end）")
        return self

    def segment_dicts(self) -> list[dict]:
        if self.segments:
            return [{"start": item.start.isoformat(), "end": item.end.isoformat()} for item in self.segments]
        return [{"start": self.interval_start.isoformat(), "end": self.interval_end.isoformat()}]


class SrmCheckIn(BaseModel):
    """已弃用：SRM 不再单独重算，统一走 POST /experiments/{id}/analysis-runs。"""

    alpha: float | None = Field(default=None, gt=0, lt=0.5)
    power: float | None = Field(default=None, gt=0, lt=1)
    alarm_p: float | None = Field(default=None, gt=0, lt=0.5)


class MetricCheckIn(BaseModel):
    """已弃用：指标检验不再脱离分析版本单独重算。"""

    method: Literal["bh", "bonferroni"] = "bh"
    alpha: float | None = Field(default=None, gt=0, lt=0.5)
    variant_code: str | None = Field(default=None, max_length=40)
