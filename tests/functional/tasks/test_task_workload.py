"""A task handler runs as the workload it was registered with."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from family_assistant.llm.call_context import Workload, current_workload
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority
from family_assistant.task_worker import TaskWorker
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.idle_processing_service import make_idle_processing_service
from tests.mocks.recording_chat_interface import RecordingChatInterface

type TaskWorkerFactory = Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]]


async def _workload_seen_by(
    task_worker_manager: TaskWorkerFactory,
    task_type: str,
    workload: Workload | None,
    payload: dict[str, str] | None = None,
) -> Workload:
    worker, new_task_event, _shutdown_event = task_worker_manager(
        processing_service=make_idle_processing_service(),
        chat_interface=RecordingChatInterface(),
    )
    engine = worker.engine
    assert engine is not None
    seen: list[Workload] = []

    async def handler(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - the TaskHandler signature types payloads this way
        task_payload: dict[str, Any],
    ) -> None:
        seen.append(current_workload())

    if workload is None:
        worker.register_task_handler(task_type, handler)
    else:
        worker.register_task_handler(task_type, handler, workload=workload)
    await Database(engine=engine).tasks.enqueue(
        task_id=f"workload_{task_type}",
        task_type=task_type,
        payload=payload or {},
        priority=TaskPriority.INTERACTIVE,
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(
        engine=engine, timeout_seconds=10.0, task_ids={f"workload_{task_type}"}
    )
    assert len(seen) == 1
    return seen[0]


@pytest.mark.asyncio
async def test_a_handler_registered_as_background_runs_as_background(
    task_worker_manager: TaskWorkerFactory,
) -> None:
    workload = await _workload_seen_by(
        task_worker_manager, "index_something", Workload.BACKGROUND
    )

    assert workload is Workload.BACKGROUND


@pytest.mark.asyncio
async def test_a_background_turn_task_runs_as_background(
    task_worker_manager: TaskWorkerFactory,
) -> None:
    """Background turns leave the worker for the turn runner, and keep it."""
    workload = await _workload_seen_by(
        task_worker_manager,
        "llm_callback",
        Workload.BACKGROUND,
        payload={"interface_type": "web", "conversation_id": "workload-test"},
    )

    assert workload is Workload.BACKGROUND


@pytest.mark.asyncio
async def test_a_handler_registered_without_a_workload_is_interactive(
    task_worker_manager: TaskWorkerFactory,
) -> None:
    workload = await _workload_seen_by(task_worker_manager, "unclassified", None)

    assert workload is Workload.INTERACTIVE
