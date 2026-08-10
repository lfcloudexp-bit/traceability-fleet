"""Agent Observability - OpenTelemetry spans plus reasoning-chain traces.

Two layers, because they answer different questions.

OpenTelemetry answers "what ran, where, and how long", exported to Cloud Trace
like any other service. The reasoning chain answers "why did the fleet decide
that", which no APM tool captures: every step records its inputs, its outputs
and its rationale, keyed to the same trace id.
"""
import os, datetime, uuid, contextlib
from google.cloud import firestore

PROJECT = os.environ["GCP_PROJECT"]
_db = firestore.Client(project=PROJECT)
OTEL_ERROR = None

try:
    from opentelemetry import trace as _otel
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    _provider = TracerProvider(
        resource=Resource.create({"service.name": "traceability-fleet"}))
    _provider.add_span_processor(
        BatchSpanProcessor(CloudTraceSpanExporter(project_id=PROJECT)))
    _otel.set_tracer_provider(_provider)
    _tracer = _otel.get_tracer("traceability-fleet")
    OTEL_ENABLED = True
except Exception as exc:
    _tracer = None
    OTEL_ENABLED = False
    OTEL_ERROR = f"{type(exc).__name__}: {exc}"[:200]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@contextlib.contextmanager
def span(name, **attributes):
    """OpenTelemetry span. Degrades to a no-op if the exporter is unavailable."""
    if not OTEL_ENABLED:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attributes.items():
            s.set_attribute(k, str(v))
        yield s


def current_trace_id():
    if not OTEL_ENABLED:
        return None
    ctx = _otel.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.trace_id else None


def new_run(project, entrypoint, actor="gateway"):
    run_id = uuid.uuid4().hex[:12]
    _db.collection("runs").document(run_id).set({
        "run_id": run_id, "project": project, "entrypoint": entrypoint,
        "actor": actor, "started_at": _now(), "trace_id": current_trace_id(),
    })
    return run_id


def record_step(run_id, agent, step, rationale, inputs=None, outputs=None,
                decision=None):
    """One link of the reasoning chain. Append-only, ordered by seq.

    The rationale is the point. An audit two years from now needs to know not
    only that a trace link was created, but on what grounds.
    """
    col = _db.collection("runs").document(run_id).collection("reasoning")
    seq = len(list(col.limit(500).stream())) + 1
    col.document(f"{seq:04d}").set({
        "seq": seq, "agent": agent, "step": step, "rationale": rationale,
        "inputs": inputs or {}, "outputs": outputs or {}, "decision": decision,
        "trace_id": current_trace_id(), "ts": _now(),
    })
    return seq


def finish_run(run_id, status="completed", summary=None):
    _db.collection("runs").document(run_id).set(
        {"status": status, "finished_at": _now(), "summary": summary or {}},
        merge=True)


def chain(run_id):
    """Full reasoning chain for a run, in order. This is what an auditor reads."""
    col = _db.collection("runs").document(run_id).collection("reasoning")
    return sorted((d.to_dict() for d in col.stream()), key=lambda s: s["seq"])
