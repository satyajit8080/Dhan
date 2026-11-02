"""Research laboratory API (requires PostgreSQL)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from doi.app.security import requires
from doi.core.auth import Principal
from doi.data.historical_ingestion.runner import DatasetNotApproved, coverage_report, load_approved_dataset
from doi.research.lab import Lab, LabError, StrategySpec

router = APIRouter(prefix="/api/v1/research")


def _lab(request: Request) -> tuple:
    rt = request.app.state.runtime
    if rt.paper_store is None:
        raise HTTPException(503, "the research lab requires DATABASE_URL")
    if getattr(rt, "lab", None) is None:
        rt.lab = Lab(rt.paper_store.dsn, calendar=rt.paper.cal)
    return rt, rt.lab


class VersionIn(BaseModel):
    spec: StrategySpec
    parent_version_id: str | None = None


class ExperimentIn(BaseModel):
    version_id: str
    dataset_id: str
    allow_gaps: bool = False


class NoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class RollbackIn(NoteIn):
    to_version_id: str


def _err(e: Exception) -> HTTPException:
    return HTTPException(409 if isinstance(e, LabError) else 422, str(e))


@router.post("/strategies", status_code=201)
async def create_version(request: Request, body: VersionIn, who: Principal = Depends(requires("approver"))):
    _rt, lab = _lab(request)
    vid = await asyncio.to_thread(lab.create_version, body.spec, f"human:{who.actor}", body.parent_version_id)
    return {"version_id": vid, "spec_sha256": body.spec.sha256}


@router.get("/strategies/{strategy_id}")
async def history(request: Request, strategy_id: str, who: Principal = Depends(requires("viewer"))):
    _rt, lab = _lab(request)
    return jsonable_encoder(await asyncio.to_thread(lab.history, strategy_id))


@router.post("/experiments", status_code=201)
async def run_experiment(request: Request, body: ExperimentIn, who: Principal = Depends(requires("approver"))):
    rt, lab = _lab(request)
    try:
        _, bars = await asyncio.to_thread(load_approved_dataset, rt.paper_store.dsn, body.dataset_id, allow_gaps=body.allow_gaps)
        return jsonable_encoder(
            await asyncio.to_thread(
                lab.run_experiment, body.version_id, bars, dataset_id=body.dataset_id, data_class="HISTORICAL"
            )
        )
    except (DatasetNotApproved, LabError) as e:
        raise _err(e) from None


@router.post("/experiments/{experiment_id}/holdout")
async def holdout(request: Request, experiment_id: str, dataset_id: str, who: Principal = Depends(requires("approver"))):
    rt, lab = _lab(request)
    try:
        _, bars = await asyncio.to_thread(load_approved_dataset, rt.paper_store.dsn, dataset_id)
        return jsonable_encoder(await asyncio.to_thread(lab.open_holdout, experiment_id, f"human:{who.actor}", bars))
    except (DatasetNotApproved, LabError) as e:
        raise _err(e) from None


@router.post("/experiments/{experiment_id}/paper-validation")
async def paper_validation(request: Request, experiment_id: str, who: Principal = Depends(requires("approver"))):
    rt, lab = _lab(request)
    try:
        return await asyncio.to_thread(lab.record_paper_validation, experiment_id, list(rt.paper.trades))
    except LabError as e:
        raise _err(e) from None


@router.post("/experiments/{experiment_id}/promote")
async def promote(request: Request, experiment_id: str, body: NoteIn | None = None, who: Principal = Depends(requires("admin"))):
    rt, lab = _lab(request)
    try:
        await asyncio.to_thread(lab.promote, experiment_id, who, rt.approvals, body.note if body else None)
    except LabError as e:
        raise _err(e) from None
    return {"promoted": experiment_id}


@router.post("/strategies/{strategy_id}/rollback")
async def rollback(request: Request, strategy_id: str, body: RollbackIn, who: Principal = Depends(requires("admin"))):
    rt, lab = _lab(request)
    try:
        await asyncio.to_thread(lab.rollback, strategy_id, body.to_version_id, who, rt.approvals, body.note)
    except LabError as e:
        raise _err(e) from None
    return {"active_version_id": body.to_version_id}


@router.get("/coverage")
async def coverage(request: Request, who: Principal = Depends(requires("viewer"))):
    rt, _ = _lab(request)
    return jsonable_encoder(await asyncio.to_thread(coverage_report, rt.paper_store.dsn))
