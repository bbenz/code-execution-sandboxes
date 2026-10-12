"""FastAPI orchestrator: serves the race board and drives referee races on the chosen sandbox backend."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import telemetry
from backends import build_backend
from catalog import TargetCatalog
from events import RunRegistry
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from generators.recorded import RecordedGenerator
from pydantic import BaseModel, Field
from race import Race
from settings import Settings

logger = logging.getLogger("ces.app")
STATIC_DIR = Path(__file__).resolve().parent / "static"
# The board's files change with every deploy. Make browsers revalidate them (cheap 304s) instead of
# running a cached, stale app.js against a newer API.
NO_CACHE = {"Cache-Control": "no-cache"}


class RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


class RaceRequest(BaseModel):
    target: str
    generator: str = Field("auto", pattern="^(auto|llm|recorded)$")
    waves: int = Field(3, ge=1, le=5)
    candidates_per_wave: int = Field(4, ge=1, le=8)
    inject_rogue: bool = False


class BenchmarkRequest(BaseModel):
    target: str = "java-ratelimiter"


class App:
    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.catalog = TargetCatalog(self.settings.targets_dir)
        self.recorded = RecordedGenerator(self.settings.recorded_dir)
        self.runs = RunRegistry()
        self.backend = None
        self.telemetry_on = False
        self._tasks: set[asyncio.Task] = set()

    async def startup(self) -> None:
        self.telemetry_on = telemetry.setup(self.settings.appinsights_connection_string)
        self.backend = build_backend(self.settings)
        await self.backend.startup()
        logger.info("Backend %s ready; targets: %s", self.backend.name,
                    ", ".join(t.name for t in self.catalog.all()))

    def generator_for(self, name: str):
        want_llm = name == "llm" or (name == "auto" and self.settings.llm_configured)
        if want_llm:
            if not self.settings.llm_configured:
                raise HTTPException(400, "No model configured (set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT).")
            from generators.llm import LlmGenerator

            return LlmGenerator(self.settings.openai_endpoint, self.settings.openai_deployment)
        return self.recorded

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


app_state = App()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await app_state.startup()
    yield
    for task in list(app_state._tasks):
        task.cancel()


app = FastAPI(title="Code Execution Sandboxes — Referee", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "backend": app_state.settings.backend}


@app.get("/api/config")
async def config():
    backend = app_state.backend
    targets = app_state.catalog.all()
    return {
        "backend": backend.describe() if backend else {"name": app_state.settings.backend},
        "targets": [target.public() for target in targets],
        "recorded_targets": [target.name for target in targets if app_state.recorded.has_recordings(target.name)],
        "llm_configured": app_state.settings.llm_configured,
        "telemetry": app_state.telemetry_on,
        "tenant": app_state.settings.tenant,
        "max_parallel": app_state.settings.max_parallel,
    }


@app.get("/api/targets/{name}")
async def target_detail(name: str):
    try:
        target = app_state.catalog.get(name)
    except KeyError:
        raise HTTPException(404, f"Unknown target: {name}") from None
    files = {path: target.read(path) for path in (*target.editable, *target.context)}
    return {**target.public(), "files": files}


async def _require_backend_image(*roles: str) -> None:
    # Re-discover first, so images rebuilt by prepare_images.py are used without restarting the app.
    await app_state.backend.refresh_images()
    for role in roles:
        if not app_state.backend.has_image(role):
            raise HTTPException(409, f"The '{role}' engine image isn't ready. Run scripts/prepare_images.py "
                                     f"(or build it locally) before starting a race.")


@app.post("/api/race")
async def start_race(request: RaceRequest):
    try:
        target = app_state.catalog.get(request.target)
    except KeyError:
        raise HTTPException(404, f"Unknown target: {request.target}") from None
    await _require_backend_image("primed")
    generator = app_state.generator_for(request.generator)

    extra = []
    if request.inject_rogue and generator.name == "llm":
        extra = app_state.recorded.injected(target.name)

    state = app_state.runs.create("race", request.model_dump())
    race = Race(state=state, settings=app_state.settings, backend=app_state.backend, target=target,
                generator=generator, max_waves=request.waves, candidates_per_wave=request.candidates_per_wave,
                extra_first_wave=extra)
    app_state.spawn(race.run())
    return {"run_id": state.run_id}


@app.post("/api/benchmark")
async def start_benchmark(request: BenchmarkRequest):
    try:
        target = app_state.catalog.get(request.target)
    except KeyError:
        raise HTTPException(404, f"Unknown target: {request.target}") from None
    candidate = app_state.recorded.benchmark_candidate(target.name)
    if candidate is None:
        raise HTTPException(400, f"No benchmark candidate recorded for {target.name}.")
    await _require_backend_image("primed", "base")
    state = app_state.runs.create("benchmark", request.model_dump())
    race = Race(state=state, settings=app_state.settings, backend=app_state.backend, target=target,
                generator=app_state.recorded)
    app_state.spawn(race.benchmark(candidate))
    return {"run_id": state.run_id}


@app.post("/api/sweep")
async def sweep():
    deleted = await app_state.backend.sweep()
    return {"deleted": deleted}


@app.get("/api/runs/{run_id}")
async def run_snapshot(run_id: str):
    state = app_state.runs.get(run_id)
    if state is None:
        raise HTTPException(404, "Unknown run")
    return {"run_id": run_id, "kind": state.kind, "done": state.done, "result": state.result,
            "events": state.events}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, after: int = 0, last_event_id: str | None = Header(None)):
    state = app_state.runs.get(run_id)
    if state is None:
        raise HTTPException(404, "Unknown run")
    # A reconnecting EventSource sends the last id it saw; resume after it instead of replaying the run.
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    async def event_stream():
        import json

        async for event in state.stream(after=after):
            if event is None:
                yield ": keep-alive\n\n"
            else:
                yield f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
async def index():
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        return JSONResponse({"service": "code-execution-sandboxes referee"})
    return FileResponse(page, headers=NO_CACHE)


if STATIC_DIR.is_dir():
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")
