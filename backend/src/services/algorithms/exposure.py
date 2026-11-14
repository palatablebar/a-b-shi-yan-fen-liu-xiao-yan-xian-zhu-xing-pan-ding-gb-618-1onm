"""可用曝光区间：纯函数，无 IO。

一次分析只认负责人登记的一组「可用曝光区间」——中途埋点故障、暂停、补数的日期
不进入这组区间。所有分流校验 / 指标检验 / 决策都按同一组区间聚合，避免把故障期
与前后两次运行累加在一起。

口径：
- 区间按日期闭区间 [start, end] 计算；
- 允许存在多段不连续区间（例如 06-01~06-09 + 06-12~06-15，跳过 06-10/06-11）；
- 段内不合法（start > end）或任意两段重叠 → ValueError；
- 归一化后按开始日期升序，并生成稳定的 interval_key：
  "2026-06-01~2026-06-09|2026-06-12~2026-06-15"
同一组区间重复登记时 key 完全一致，落库层据此复用同一个分析版本。
"""
from __future__ import annotations

from datetime import date


def _parse_date(value: object, field: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"{field} 必须是 YYYY-MM-DD") from exc
    raise ValueError(f"{field} 必须是日期")


def normalize_segments(raw: object) -> list[dict]:
    """把任意输入归一为 [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, ...]。"""
    if not raw:
        raise ValueError("至少登记一段可用曝光区间")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("segments 必须是区间数组")
    segments: list[tuple[date, date]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            start_raw = item.get("start", item.get("start_date"))
            end_raw = item.get("end", item.get("end_date"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start_raw, end_raw = item
        else:
            raise ValueError(f"第 {index + 1} 段区间格式不合法")
        start = _parse_date(start_raw, f"第 {index + 1} 段 start")
        end = _parse_date(end_raw, f"第 {index + 1} 段 end")
        if end < start:
            raise ValueError(f"第 {index + 1} 段区间结束日期早于开始日期")
        segments.append((start, end))

    segments.sort(key=lambda pair: (pair[0], pair[1]))
    for previous, current in zip(segments, segments[1:]):
        if current[0] <= previous[1]:
            raise ValueError(
                f"可用曝光区间不能重叠：{previous[0].isoformat()}~{previous[1].isoformat()} "
                f"与 {current[0].isoformat()}~{current[1].isoformat()}"
            )
    return [{"start": start.isoformat(), "end": end.isoformat()} for start, end in segments]


def segment_bounds(segments: list[dict] | None) -> list[tuple[date, date]]:
    """归一化区间 -> [(start_date, end_date), ...]，供 SQL 过滤与序列化使用。"""
    if not segments:
        return []
    return [(_parse_date(item["start"], "start"), _parse_date(item["end"], "end")) for item in segments]


def interval_key(segments: list[dict]) -> str:
    """稳定指纹：同一组区间（顺序无关）得到同一个 key。"""
    normalized = normalize_segments(segments)
    return "|".join(f"{item['start']}~{item['end']}" for item in normalized)


def interval_label(segments: list[dict] | None) -> str:
    if not segments:
        return ""
    return " · ".join(f"{item['start']}~{item['end']}" for item in segments)


def total_days(segments: list[dict] | None) -> int:
    return sum((end - start).days + 1 for start, end in segment_bounds(segments))


def contains(segments: list[dict] | None, day: date) -> bool:
    return any(start <= day <= end for start, end in segment_bounds(segments))


def single_interval(start: object, end: object) -> list[dict]:
    """`interval_start` / `interval_end` 简写 -> 标准 segments。"""
    return normalize_segments([{"start": start, "end": end}])
