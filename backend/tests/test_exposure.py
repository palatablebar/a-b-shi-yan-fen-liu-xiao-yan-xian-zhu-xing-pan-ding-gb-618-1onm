"""可用曝光区间纯函数：归一化 / 重叠 / 稳定指纹 / 包含判断。"""
from __future__ import annotations

import pytest

from src.services.algorithms import exposure


def test_normalizes_and_sorts_segments():
    segments = exposure.normalize_segments(
        [
            {"start": "2026-06-12", "end": "2026-06-15"},
            {"start": "2026-06-01", "end": "2026-06-09"},
        ]
    )
    assert segments == [
        {"start": "2026-06-01", "end": "2026-06-09"},
        {"start": "2026-06-12", "end": "2026-06-15"},
    ]
    assert exposure.interval_key(segments) == "2026-06-01~2026-06-09|2026-06-12~2026-06-15"


def test_key_is_order_independent_and_seed_stable():
    a = [{"start": "2026-06-01", "end": "2026-06-09"}, {"start": "2026-06-12", "end": "2026-06-15"}]
    b = [{"start": "2026-06-12", "end": "2026-06-15"}, {"start": "2026-06-01", "end": "2026-06-09"}]
    assert exposure.interval_key(a) == exposure.interval_key(b)
    assert exposure.interval_key(a) == exposure.interval_key(exposure.normalize_segments(a))


def test_rejects_overlap_reversed_and_empty():
    with pytest.raises(ValueError):
        exposure.normalize_segments([{"start": "2026-06-01", "end": "2026-06-09"}, {"start": "2026-06-09", "end": "2026-06-12"}])
    with pytest.raises(ValueError):
        exposure.normalize_segments([{"start": "2026-06-09", "end": "2026-06-01"}])
    with pytest.raises(ValueError):
        exposure.normalize_segments([])
    with pytest.raises(ValueError):
        exposure.normalize_segments(None)


def test_adjacent_segments_allowed_but_touching_not():
    """06-01~06-09 与 06-10~06-12 相邻合法；06-09~06-10 与 06-10~ 重叠非法。"""
    adjacent = exposure.normalize_segments(
        [{"start": "2026-06-01", "end": "2026-06-09"}, {"start": "2026-06-10", "end": "2026-06-12"}]
    )
    assert exposure.total_days(adjacent) == 12
    with pytest.raises(ValueError):
        exposure.normalize_segments(
            [{"start": "2026-06-01", "end": "2026-06-10"}, {"start": "2026-06-10", "end": "2026-06-12"}]
        )


def test_contains_respects_gaps():
    segments = exposure.normalize_segments(
        [{"start": "2026-06-01", "end": "2026-06-09"}, {"start": "2026-06-12", "end": "2026-06-15"}]
    )
    from datetime import date

    assert exposure.contains(segments, date(2026, 6, 5)) is True
    assert exposure.contains(segments, date(2026, 6, 10)) is False  # 故障期
    assert exposure.contains(segments, date(2026, 6, 16)) is False
    assert exposure.single_interval("2026-06-01", "2026-06-03")[0]["end"] == "2026-06-03"
