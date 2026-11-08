"""Start workflows and manage schedules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleSpec,
    ScheduleUpdate,
    WorkflowExecutionStatus,
)

from ..config import get_settings
from .types import MAIN_QUEUE, PipelineInput
from .workflows import PipelineWorkflow

SCHEDULE_ID = "croissant-pipeline-nightly"


async def get_client() -> Client:
    s = get_settings()
    return await Client.connect(s.temporal_address, namespace=s.temporal_namespace)


async def start_pipeline(sources: list[str], limit: int, *, enrich: bool = True, graph: bool = True,
                         discover: bool = True, wait: bool = False) -> dict[str, Any]:
    client = await get_client()
    wid = f"pipeline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    handle = await client.start_workflow(
        PipelineWorkflow.run,
        PipelineInput(sources=sources, limit=limit, discover=discover, enrich=enrich, graph=graph),
        id=wid, task_queue=MAIN_QUEUE,
    )
    out: dict[str, Any] = {"workflow_id": wid, "run_id": handle.result_run_id,
                           "ui": f"{get_settings().temporal_ui_url}/namespaces/default/workflows/{wid}"}
    if wait:
        out["result"] = await handle.result()
    return out


async def upsert_schedule(cron: str, limit: int, delete: bool = False) -> dict[str, Any]:
    client = await get_client()
    handle = client.get_schedule_handle(SCHEDULE_ID)
    if delete:
        await handle.delete()
        return {"deleted": SCHEDULE_ID}
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            PipelineWorkflow.run,
            PipelineInput(sources=get_settings().sources_enabled, limit=limit),
            id="scheduled-pipeline", task_queue=MAIN_QUEUE),
        spec=ScheduleSpec(cron_expressions=[cron]),
    )
    try:
        await client.create_schedule(SCHEDULE_ID, schedule)
        return {"created": SCHEDULE_ID, "cron": cron}
    except ScheduleAlreadyRunningError:
        await handle.update(lambda _: ScheduleUpdate(schedule=schedule))
        return {"updated": SCHEDULE_ID, "cron": cron}


async def recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    client = await get_client()
    out = []
    async for wf in client.list_workflows("WorkflowType = 'PipelineWorkflow'", limit=limit):
        out.append({
            "id": wf.id, "run_id": wf.run_id, "status": WorkflowExecutionStatus(wf.status).name if wf.status else None,
            "start_time": wf.start_time, "close_time": wf.close_time,
        })
        if len(out) >= limit:
            break
    return out
