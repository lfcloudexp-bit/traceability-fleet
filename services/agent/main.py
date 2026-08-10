"""Agent Runtime - one worker image, four roles, selected per deployment.

Each agent runs the same audited artefact under its own service account and
its own Pub/Sub subscription. The role is injected at deploy time, never
chosen at runtime, so a compromised worker cannot promote itself into another
agent's scopes.
"""
import os, json, base64
from fastapi import FastAPI, Request, HTTPException
from google import genai
from common import registry, memory, telemetry, llm

PROJECT = os.environ["GCP_PROJECT"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
AGENT_ID = os.environ["AGENT_ID"]

_client = genai.Client(vertexai=True, project=PROJECT, location="global")
app = FastAPI(title=f"Traceability Fleet - agent:{AGENT_ID}")

JSON_RULE = ("Answer ONLY with valid JSON, no prose and no code fences. "
             "Always write in English.")


def _gemini_json(prompt):
    registry.authorize(AGENT_ID, "gemini.generate")
    raw = llm.generate(prompt)
    cleaned = raw.strip()
    for fence in ("```json", "```"):
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())


def run_ingestor(run_id, project, text):
    reqs = _gemini_json(
        "You are a requirements engineer. Extract ATOMIC requirements from the "
        "specification below: one obligation each, no compound statements. For "
        "each, decide whether it is objectively verifiable, meaning it states a "
        "measurable criterion. "
        'Schema: [{"id":"REQ-###","text":"...","type":"functional|non-functional",'
        '"verifiable":true|false,"reason":"why verifiable or not"}]. '
        + JSON_RULE + "\n\nSPECIFICATION:\n" + text)
    for r in reqs:
        memory.write(AGENT_ID, "requirements", f"{project}-{r['id']}", r, project)
    weak = [r["id"] for r in reqs if r.get("verifiable") is False]
    telemetry.record_step(
        run_id, AGENT_ID, "extract",
        f"Split the specification into {len(reqs)} atomic requirements; "
        f"{len(weak)} state no measurable criterion",
        {"chars": len(text)}, {"requirements": len(reqs), "unverifiable": weak})
    memory.append_event(project, AGENT_ID, "requirements_extracted", {"n": len(reqs)})
    return {"requirements": len(reqs), "unverifiable": weak}


def run_linker(run_id, project, text):
    """Link requirements to the design and test artefacts supplied."""
    reqs = memory.read(AGENT_ID, "requirements", project)
    catalogue = [{"id": r.get("id"), "text": r.get("text")} for r in reqs]
    links = _gemini_json(
        "You are a systems engineer maintaining a traceability matrix. Below "
        "are existing REQUIREMENTS and a DESIGN AND TEST document. Propose a "
        "trace link only where the artefact genuinely satisfies or verifies the "
        "requirement. Do not invent artefacts, and leave a requirement unlinked "
        "rather than guessing. "
        'Schema: [{"req":"REQ-###","design":"...or null","test":"...or null",'
        '"confidence":0.0-1.0,"rationale":"one sentence"}]. '
        + JSON_RULE + "\n\nREQUIREMENTS:\n" + json.dumps(catalogue)
        + "\n\nDESIGN AND TEST:\n" + text)
    kept = [l for l in links if l.get("confidence", 0) >= 0.5
            and (l.get("design") or l.get("test"))]
    for l in kept:
        memory.write(AGENT_ID, "trace_links", f"{project}-{l['req']}", l, project)
    telemetry.record_step(
        run_id, AGENT_ID, "link",
        f"Proposed {len(links)} links over {len(catalogue)} requirements; kept "
        f"{len(kept)} at confidence >= 0.5, the rest left unlinked on purpose",
        {"requirements": len(catalogue)}, {"links": len(kept)})
    memory.append_event(project, AGENT_ID, "traces_linked", {"n": len(kept)})
    return {"links": len(kept), "considered": len(links)}


def run_auditor(run_id, project, text=""):
    """Find what is broken. Orphans are computed, not guessed."""
    reqs = memory.read(AGENT_ID, "requirements", project)
    links = memory.read(AGENT_ID, "trace_links", project)
    linked = {l.get("req") for l in links}

    findings = []
    # Deterministic checks first: no model should be asked what a join can answer.
    for r in reqs:
        rid = r.get("id")
        if rid not in linked:
            findings.append({"kind": "orphan", "req": rid, "severity": "high",
                             "detail": "No design or test artefact traces to this requirement",
                             "detector": "deterministic"})
        if r.get("verifiable") is False:
            findings.append({"kind": "not_verifiable", "req": rid, "severity": "medium",
                             "detail": r.get("reason", "No measurable criterion stated"),
                             "detector": "deterministic"})

    # Only then the model, for what genuinely needs judgement.
    if len(reqs) > 1:
        semantic = _gemini_json(
            "You are auditing a requirement set. Report ONLY genuine "
            "contradictions or overlaps between requirements. If there are "
            "none, return an empty array. "
            'Schema: [{"kind":"contradiction|overlap","reqs":["REQ-###"],'
            '"severity":"high|medium|low","detail":"one sentence"}]. '
            + JSON_RULE + "\n\nREQUIREMENTS:\n"
            + json.dumps([{"id": r.get("id"), "text": r.get("text")} for r in reqs]))
        for s in semantic:
            s["detector"] = "model"
            findings.append(s)

    for i, f in enumerate(findings, 1):
        memory.write(AGENT_ID, "findings", f"{project}-F{i:03d}", f, project)
    by_kind = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    telemetry.record_step(
        run_id, AGENT_ID, "audit",
        f"{len(reqs)} requirements and {len(links)} links reviewed; "
        f"{len(findings)} findings: {by_kind}",
        {"requirements": len(reqs), "links": len(links)}, by_kind)
    memory.append_event(project, AGENT_ID, "audit_completed", by_kind)
    return {"findings": len(findings), "by_kind": by_kind}


def run_linker(run_id, project, text):
    """Link requirements to the design and test artefacts supplied."""
    reqs = memory.read(AGENT_ID, "requirements", project)
    catalogue = [{"id": r.get("id"), "text": r.get("text")} for r in reqs]
    links = _gemini_json(
        "You are a systems engineer maintaining a traceability matrix. Below "
        "are existing REQUIREMENTS and a DESIGN AND TEST document. Propose a "
        "trace link only where the artefact genuinely satisfies or verifies the "
        "requirement. Do not invent artefacts, and leave a requirement unlinked "
        "rather than guessing. "
        'Schema: [{"req":"REQ-###","design":"...or null","test":"...or null",'
        '"confidence":0.0-1.0,"rationale":"one sentence"}]. '
        + JSON_RULE + "\n\nREQUIREMENTS:\n" + json.dumps(catalogue)
        + "\n\nDESIGN AND TEST:\n" + text)
    kept = [l for l in links if l.get("confidence", 0) >= 0.5
            and (l.get("design") or l.get("test"))]
    for l in kept:
        memory.write(AGENT_ID, "trace_links", f"{project}-{l['req']}", l, project)
    telemetry.record_step(
        run_id, AGENT_ID, "link",
        f"Proposed {len(links)} links over {len(catalogue)} requirements; kept "
        f"{len(kept)} at confidence >= 0.5, the rest deliberately left unlinked",
        {"requirements": len(catalogue)}, {"links": len(kept)})
    memory.append_event(project, AGENT_ID, "traces_linked", {"n": len(kept)})
    return {"links": len(kept), "considered": len(links)}


def run_auditor(run_id, project, text=""):
    """Find what is broken. Orphans are computed, not guessed."""
    reqs = memory.read(AGENT_ID, "requirements", project)
    links = memory.read(AGENT_ID, "trace_links", project)
    linked = {l.get("req") for l in links}

    findings = []
    # Deterministic checks first: never ask a model what a join can answer.
    for r in reqs:
        rid = r.get("id")
        if rid not in linked:
            findings.append({"kind": "orphan", "reqs": [rid], "severity": "high",
                             "detail": "No design or test artefact traces to this requirement",
                             "detector": "deterministic"})
        if r.get("verifiable") is False:
            findings.append({"kind": "not_verifiable", "reqs": [rid], "severity": "medium",
                             "detail": r.get("reason", "No measurable criterion stated"),
                             "detector": "deterministic"})

    # Only then the model, and only for what genuinely needs judgement.
    if len(reqs) > 1:
        for s in _gemini_json(
                "You are auditing a requirement set. Report ONLY genuine "
                "contradictions or overlaps between requirements. If there are "
                "none, return an empty array. "
                'Schema: [{"kind":"contradiction|overlap","reqs":["REQ-###"],'
                '"severity":"high|medium|low","detail":"one sentence"}]. '
                + JSON_RULE + "\n\nREQUIREMENTS:\n"
                + json.dumps([{"id": r.get("id"), "text": r.get("text")} for r in reqs])):
            s["detector"] = "model"
            findings.append(s)

    for i, f in enumerate(findings, 1):
        memory.write(AGENT_ID, "findings", f"{project}-F{i:03d}", f, project)
    by_kind = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    telemetry.record_step(
        run_id, AGENT_ID, "audit",
        f"Reviewed {len(reqs)} requirements against {len(links)} links; "
        f"{len(findings)} findings {by_kind}",
        {"requirements": len(reqs), "links": len(links)}, by_kind)
    memory.append_event(project, AGENT_ID, "audit_completed", by_kind)
    return {"findings": len(findings), "by_kind": by_kind}


def run_impact(run_id, project, text):
    """Given a change, trace what breaks downstream."""
    state = {
        "requirements": memory.read(AGENT_ID, "requirements", project),
        "trace_links": memory.read(AGENT_ID, "trace_links", project),
        "findings": memory.read(AGENT_ID, "findings", project),
    }
    report = _gemini_json(
        "You are a change manager. Given the CHANGE below and the current "
        "traceability state, list what is affected downstream and what must be "
        "re-verified. Be specific and do not speculate beyond the data. "
        'Schema: {"summary":"one sentence","affected":[{"req":"REQ-###",'
        '"impact":"...","action":"re-verify|re-design|no action"}]}. '
        + JSON_RULE + "\n\nCHANGE:\n" + text + "\n\nSTATE:\n"
        + json.dumps(state, default=str)[:12000])
    memory.write(AGENT_ID, "impact_reports", f"{project}-{run_id}",
                 {"change": text[:2000], **report}, project)
    n = len(report.get("affected", []))
    telemetry.record_step(run_id, AGENT_ID, "assess_impact",
                          report.get("summary", "impact assessed"),
                          {"requirements": len(state["requirements"])},
                          {"affected": n})
    memory.append_event(project, AGENT_ID, "impact_assessed", {"affected": n})
    return {"affected": n, "summary": report.get("summary", "")}


HANDLERS = {"ingestor": run_ingestor, "linker": run_linker,
            "auditor": run_auditor, "impact": run_impact}


@app.get("/")
def health():
    m = registry.get(AGENT_ID) or {}
    return {"status": "ok", "agent": AGENT_ID, "version": m.get("version"),
            "department": m.get("department"), "model": MODEL,
            "allowed_tools": m.get("allowed_tools", [])}


@app.post("/pubsub")
async def consume(request: Request):
    """Push endpoint for this agent's own subscription."""
    envelope = await request.json()
    raw = (envelope or {}).get("message", {}).get("data", "")
    data = json.loads(base64.b64decode(raw).decode())
    if data.get("agent") != AGENT_ID:
        return {"skipped": data.get("agent")}
    run_id, project = data["run_id"], data["project"]
    with telemetry.span(f"agent.{AGENT_ID}", project=project, run=run_id):
        try:
            if AGENT_ID == "ingestor" and data.get("requirements"):
                result = run_ingestor_structured(run_id, project,
                                                 data["requirements"])
            else:
                result = HANDLERS[AGENT_ID](run_id, project, data.get("text", ""))
            telemetry.finish_run(run_id, "completed", result)
            return {"ok": True, "agent": AGENT_ID, **result}
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:300]
            telemetry.record_step(run_id, AGENT_ID, "error", detail, {}, {}, "ERROR")
            telemetry.finish_run(run_id, "failed", {"error": detail})
            raise HTTPException(500, detail)


def run_ingestor_structured(run_id, project, incoming):
    """Requirements that arrived already atomic, from a spreadsheet or ReqIF.

    Nothing is decomposed and nothing is renumbered. Renaming a customer
    identifier destroys upward traceability, which is the whole point. The only
    judgement left is whether each requirement states a criterion a test could
    actually verify.
    """
    verdicts = _gemini_json(
        "You are a requirements engineer reviewing a customer specification. "
        "For each requirement decide whether it is objectively verifiable, "
        "meaning it states a measurable criterion a test could check. Return "
        "exactly one entry per input requirement, echoing the id unchanged. "
        'Schema: [{"id":"<id exactly as given>","verifiable":true|false,'
        '"reason":"one sentence"}]. '
        + JSON_RULE + "\n\nREQUIREMENTS:\n"
        + json.dumps([{"id": r["id"], "text": r["text"]} for r in incoming]))
    by_id = {v.get("id"): v for v in verdicts}

    weak = []
    for r in incoming:
        v = by_id.get(r["id"], {})
        doc = dict(r)
        doc["verifiable"] = v.get("verifiable")
        doc["reason"] = v.get("reason", "")
        memory.write(AGENT_ID, "requirements", f"{project}-{r['id']}", doc, project)
        if v.get("verifiable") is False:
            weak.append(r["id"])

    preserved = sum(1 for r in incoming if r.get("id_origin") == "preserved")
    telemetry.record_step(
        run_id, AGENT_ID, "assess",
        f"Reviewed {len(incoming)} pre-structured requirements without "
        f"decomposing or renumbering them; {preserved} carry the customer's own "
        f"identifier; {len(weak)} state no measurable criterion",
        {"incoming": len(incoming)}, {"unverifiable": weak})
    memory.append_event(project, AGENT_ID, "requirements_assessed",
                        {"n": len(incoming), "unverifiable": len(weak)})
    return {"requirements": len(incoming), "unverifiable": weak,
            "preserved_ids": preserved}
