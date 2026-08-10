"""Agent Gateway - single entry point and policy enforcement point.

Nothing reaches an agent except through here. Every request is screened by
Model Armor, authorised against the Registry, traced, and only then dispatched
onto the asynchronous runtime. Agents hold no public endpoint of their own.
"""
import os, json
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from google.cloud import pubsub_v1
from fastapi.responses import JSONResponse
from common import registry, memory, telemetry, armor, excel_adapter, llm

PROJECT = os.environ["GCP_PROJECT"]
TOPIC = os.environ.get("TASK_TOPIC", "agent-tasks")
_publisher = pubsub_v1.PublisherClient()
_topic_path = _publisher.topic_path(PROJECT, TOPIC)

app = FastAPI(title="Traceability Fleet - Agent Gateway", version="1.0.0")


@app.exception_handler(llm.ModelUnavailable)
async def _model_unavailable(request, exc):
    """Throttling is not a bug. Say so, and say when to come back."""
    return JSONResponse(status_code=503, headers={"Retry-After": "60"},
                        content={"error": "model temporarily unavailable",
                                 "detail": str(exc)[:200]})

# A task is bound to exactly one agent and one tool. The mapping is explicit
# so a caller cannot ask for an agent-tool pair that was never designed.
TASK_ROUTING = {
    "extract_requirements": ("ingestor", "firestore.write:requirements"),
    "link_traces": ("linker", "firestore.write:trace_links"),
    "audit": ("auditor", "firestore.write:findings"),
    "assess_impact": ("impact", "firestore.write:impact_reports"),
}


class Submission(BaseModel):
    project: str
    task: str
    text: str = ""
    source: str = "api"


@app.get("/")
def health():
    return {"status": "ok", "service": "gateway", "project": PROJECT,
            "otel": telemetry.OTEL_ENABLED, "tasks": sorted(TASK_ROUTING)}


@app.get("/registry")
def discovery(department: str = None):
    """Discovery: which agents exist, who owns them, what they may touch."""
    return {"agents": registry.list_agents(department)}


@app.post("/submit")
def submit(sub: Submission):
    """Screen, authorise, dispatch. In that order, every time."""
    if sub.task not in TASK_ROUTING:
        raise HTTPException(400, f"unknown task '{sub.task}'")
    agent_id, tool = TASK_ROUTING[sub.task]

    with telemetry.span("gateway.submit", task=sub.task, agent=agent_id,
                        project=sub.project):
        run_id = telemetry.new_run(sub.project, sub.task)

        # 1. Untrusted input is screened before anything else touches it.
        v = armor.screen(sub.text, source=sub.source, agent="gateway")
        telemetry.record_step(
            run_id, "gateway", "screen",
            f"{len(v['injection_findings'])} injection finding(s); "
            f"judge={v['judge']['verdict']}: {v['judge']['reason']}",
            {"chars": v["chars"], "sha256_16": v["doc_sha256_16"]},
            {"pii_redacted": len(v["pii_findings"])}, v["decision"])
        if v["decision"] == "BLOCK":
            telemetry.finish_run(run_id, "blocked", {"reason": "model_armor"})
            raise HTTPException(422, {"error": "blocked by Model Armor",
                                      "run_id": run_id,
                                      "audit_id": v["audit_id"],
                                      "findings": v["injection_findings"],
                                      "judge": v["judge"]})

        # 2. Zero trust: the agent must be registered and hold the tool.
        try:
            manifest = registry.authorize(agent_id, tool)
        except registry.PolicyViolation as exc:
            telemetry.record_step(run_id, "gateway", "authorize", str(exc),
                                  {"agent": agent_id, "tool": tool}, {}, "DENY")
            telemetry.finish_run(run_id, "denied", {"reason": str(exc)})
            raise HTTPException(403, {"error": str(exc), "run_id": run_id})
        telemetry.record_step(
            run_id, "gateway", "authorize",
            f"{agent_id} v{manifest['version']} is approved and holds {tool}",
            {"agent": agent_id, "tool": tool}, {}, "ALLOW")

        # 3. Dispatch onto the asynchronous runtime.
        body = json.dumps({"run_id": run_id, "project": sub.project,
                           "task": sub.task, "agent": agent_id,
                           "text": v["text"],
                           "trace_id": telemetry.current_trace_id()}).encode()
        msg_id = _publisher.publish(_topic_path, body, agent=agent_id,
                                    task=sub.task).result(timeout=30)
        telemetry.record_step(run_id, "gateway", "dispatch",
                              f"queued for {agent_id} on topic {TOPIC}",
                              {"message_id": msg_id}, {}, "QUEUED")
        memory.append_event(sub.project, "gateway", "task_dispatched",
                            {"task": sub.task, "agent": agent_id, "run": run_id})

    return {"run_id": run_id, "agent": agent_id, "status": "queued",
            "armor": v["decision"], "message_id": msg_id}


@app.get("/runs/{run_id}")
def reasoning_chain(run_id: str):
    """End-to-end reasoning chain. Why the fleet decided what it decided."""
    steps = telemetry.chain(run_id)
    if not steps:
        raise HTTPException(404, f"no run '{run_id}'")
    return {"run_id": run_id, "steps": steps}


@app.get("/state/{project}")
def project_state(project: str, agent: str = "auditor"):
    """Compact state an agent resumes from after weeks away."""
    return memory.rehydrate(agent, project)


@app.post("/submit/excel")
async def submit_excel(project: str = Form(...), file: UploadFile = File(...)):
    """Ingest a customer requirements spreadsheet.

    The rows arrive already atomic, so the fleet must not re-decompose them,
    and above all must not renumber them. A spreadsheet is no less untrusted
    than a prose document, so Model Armor still screens every cell of text.
    """
    data = await file.read()
    agent_id, tool = TASK_ROUTING["extract_requirements"]

    with telemetry.span("gateway.submit_excel", project=project, agent=agent_id):
        run_id = telemetry.new_run(project, "ingest_excel")
        try:
            parsed = excel_adapter.parse(data)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:200]
            telemetry.record_step(run_id, "gateway", "parse", detail,
                                  {"filename": file.filename}, {}, "ERROR")
            telemetry.finish_run(run_id, "failed", {"error": detail})
            raise HTTPException(422, {"error": detail, "run_id": run_id})

        reqs = parsed["requirements"]
        telemetry.record_step(
            run_id, "gateway", "parse",
            f"Header on row {parsed['header_row']}; columns mapped as "
            f"{parsed['column_mapping']}; {parsed['preserved_ids']} customer "
            f"identifiers preserved and {parsed['derived_ids']} derived",
            {"filename": file.filename, "sheet": parsed["sheet"]},
            {"requirements": len(reqs)})

        v = armor.screen("\n".join(r["text"] for r in reqs),
                         source=file.filename or "upload.xlsx", agent="gateway")
        telemetry.record_step(
            run_id, "gateway", "screen",
            f"{len(v['injection_findings'])} injection finding(s); "
            f"judge={v['judge']['verdict']}: {v['judge']['reason']}",
            {"chars": v["chars"], "sha256_16": v["doc_sha256_16"]},
            {"pii_redacted": len(v["pii_findings"])}, v["decision"])
        if v["decision"] == "BLOCK":
            telemetry.finish_run(run_id, "blocked", {"reason": "model_armor"})
            raise HTTPException(422, {"error": "blocked by Model Armor",
                                      "run_id": run_id, "audit_id": v["audit_id"],
                                      "findings": v["injection_findings"]})

        try:
            manifest = registry.authorize(agent_id, tool)
        except registry.PolicyViolation as exc:
            telemetry.record_step(run_id, "gateway", "authorize", str(exc),
                                  {"agent": agent_id}, {}, "DENY")
            telemetry.finish_run(run_id, "denied", {"reason": str(exc)})
            raise HTTPException(403, {"error": str(exc), "run_id": run_id})
        telemetry.record_step(run_id, "gateway", "authorize",
                              f"{agent_id} v{manifest['version']} is approved and holds {tool}",
                              {"agent": agent_id, "tool": tool}, {}, "ALLOW")

        body = json.dumps({"run_id": run_id, "project": project,
                           "task": "extract_requirements", "agent": agent_id,
                           "requirements": reqs,
                           "trace_id": telemetry.current_trace_id()}).encode()
        msg_id = _publisher.publish(_topic_path, body, agent=agent_id,
                                    task="extract_requirements").result(timeout=30)
        telemetry.record_step(run_id, "gateway", "dispatch",
                              f"queued {len(reqs)} pre-structured requirements for {agent_id}",
                              {"message_id": msg_id}, {}, "QUEUED")
        memory.append_event(project, "gateway", "excel_ingested",
                            {"file": file.filename, "requirements": len(reqs),
                             "preserved": parsed["preserved_ids"]})

    return {"run_id": run_id, "status": "queued", "armor": v["decision"],
            "requirements": len(reqs), "preserved_ids": parsed["preserved_ids"],
            "derived_ids": parsed["derived_ids"],
            "column_mapping": parsed["column_mapping"]}
