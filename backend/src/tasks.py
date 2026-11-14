"""Celery 任务：心跳、每日 SRM 复算（对运行中的实验重算分流均衡）。"""
from __future__ import annotations

import time

from sqlalchemy import select

from src.config.celery_app import TASK_DAILY, TASK_HEARTBEAT, celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat() -> str:
    try:
        _redis().set(f"{settings.app_name}:worker:heartbeat", time.time())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
        return "error"


@celery_app.task(name=TASK_DAILY)
def daily_srm_refresh() -> dict:
    """每天 07:40 只对「有生效分析版本」的实验，在该版本区间内整套重算。

    不新增版本、不按整段累计：区间不变地重跑同一版本，结果就地覆盖；
    计算失败则沿用上一版 active 结果（run_and_publish 失败不改 active 指针）。
    """
    from src.config.database import session_scope
    from src.models.experiment import Experiment
    from src.services import analysis_service, audit_service
    from src.utils.errors import AppError

    done = 0
    skipped = 0
    with session_scope() as db:
        experiments = db.scalars(select(Experiment).where(Experiment.status == "running")).all()
        for experiment in experiments:
            version = analysis_service.active_version(db, experiment)
            if version is None:
                skipped += 1
                continue
            try:
                analysis_service.run_and_publish(
                    db,
                    experiment,
                    version=version,
                    operator_email="scheduler@" + settings.app_name + ".io",
                )
            except AppError as exc:
                skipped += 1
                logger.warning("daily analysis skip %s: %s", experiment.code, exc.code)
                analysis_service.mark_failure(db, version, exc.detail.get("reason") or exc.message)
                continue
            done += 1
            audit_service.record(
                db,
                actor_email="scheduler@" + settings.app_name + ".io",
                actor_role="admin",
                action="analysis.publish",
                entity="analysis_version",
                entity_id=version.id,
                detail={
                    "trigger": "schedule",
                    "revision": version.revision,
                    "interval_key": version.interval_key,
                    "alarm": version.srm_alarm,
                },
                layer="worker",
            )
    logger.info("daily analysis refresh finished: %s experiments, %s skipped", done, skipped)
    return {"experiments": done, "skipped": skipped}
