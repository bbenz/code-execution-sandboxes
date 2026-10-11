"""OpenTelemetry for the orchestrator. Exports to Application Insights when a connection string is set."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger("ces")

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    _tracer = trace.get_tracer("ces.orchestrator")
except ImportError:  # pragma: no cover - the orchestrator still works without OpenTelemetry
    trace = None
    _tracer = None


def setup(connection_string: str) -> bool:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not connection_string:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=connection_string, logger_name="ces")
        logger.info("Exporting traces, metrics, and logs to Application Insights")
        return True
    except Exception as error:  # observability must never take the demo down
        logger.warning("Application Insights export disabled: %s", error)
        return False


@contextmanager
def span(name: str, **attributes) -> Iterator[object]:
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as error:
            current.set_status(Status(StatusCode.ERROR, str(error)))
            raise


def set_attributes(current, **attributes) -> None:
    if current is None:
        return
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key, value)


def record_phases(phases: list[dict], anchor_ns: int, prefix: str = "referee") -> None:
    """Turn the engine's phase timings (measured inside the sandbox) into child spans of the current span.

    The sandbox needs no telemetry egress for this: the referee reports offsets, and the orchestrator
    anchors them to the moment it started the verify command.
    """
    if _tracer is None:
        return
    for phase in phases:
        start = anchor_ns + int(phase.get("start_ms", 0)) * 1_000_000
        end = start + int(phase.get("duration_ms", 0)) * 1_000_000
        child = _tracer.start_span(f"{prefix}.{phase.get('name', 'phase')}", start_time=start)
        if phase.get("detail"):
            child.set_attribute("ces.phase.detail", phase["detail"])
        child.end(end_time=end)
