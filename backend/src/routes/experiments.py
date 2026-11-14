from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import ASSIGNMENT_WRITE_ROLES, CHECK_ROLES, EXPERIMENT_WRITE_ROLES
from src.controllers import analysis_controller, assignment_controller, experiment_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.assignment import AssignmentIn
from src.schemas.experiment import AnalysisRunIn, ExperimentIn, ExperimentPatch
from src.utils.pagination import page_params

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.get("")
def list_experiments(
    q: str | None = None,
    status: str | None = None,
    primary_metric: str | None = None,
    srm_alarm: bool | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return experiment_controller.list_experiments(
        db,
        q=q,
        status=status,
        primary_metric=primary_metric,
        srm_alarm=srm_alarm,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{experiment_id}")
def get_experiment(
    experiment_id: int,
    analysis_version_id: int | None = None,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return experiment_controller.get_experiment(db, experiment_id, analysis_version_id=analysis_version_id)


@router.post("", status_code=201)
def create_experiment(
    payload: ExperimentIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*EXPERIMENT_WRITE_ROLES)),
) -> dict:
    return experiment_controller.create_experiment(db, payload.model_dump(), principal, request)


@router.patch("/{experiment_id}")
def patch_experiment(
    experiment_id: int,
    payload: ExperimentPatch,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*EXPERIMENT_WRITE_ROLES)),
) -> dict:
    return experiment_controller.update_experiment(
        db, experiment_id, payload.model_dump(exclude_unset=True), principal, request
    )


@router.get("/{experiment_id}/assignments")
def list_assignments(
    experiment_id: int,
    variant_code: str | None = None,
    channel: str | None = None,
    metric_key: str | None = None,
    analysis_version_id: int | None = None,
    limit: int = Query(60),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return assignment_controller.list_assignments(
        db,
        experiment_id,
        variant_code=variant_code,
        channel=channel,
        metric_key=metric_key,
        analysis_version_id=analysis_version_id,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{experiment_id}/samples")
def sample_matrix(
    experiment_id: int,
    metric_key: str | None = None,
    analysis_version_id: int | None = None,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return assignment_controller.matrix(db, experiment_id, metric_key, analysis_version_id)


@router.post("/{experiment_id}/assignments", status_code=201)
def create_assignment(
    experiment_id: int,
    payload: AssignmentIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*ASSIGNMENT_WRITE_ROLES)),
) -> dict:
    return assignment_controller.create_assignment(db, experiment_id, payload.model_dump(), principal, request)


@router.get("/{experiment_id}/analysis-versions")
def list_analysis_versions(
    experiment_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return analysis_controller.list_versions(db, experiment_id)


@router.get("/{experiment_id}/analysis-versions/{version_id}")
def get_analysis_version(
    experiment_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return analysis_controller.get_version(db, experiment_id, version_id)


@router.post("/{experiment_id}/analysis-runs", status_code=201)
def run_analysis(
    experiment_id: int,
    payload: AnalysisRunIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CHECK_ROLES)),
) -> dict:
    body = payload.model_dump(exclude_none=True)
    body["segments"] = payload.segment_dicts()
    body.pop("interval_start", None)
    body.pop("interval_end", None)
    return analysis_controller.run_analysis(db, experiment_id, body, principal, request)
