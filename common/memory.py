"""Memory Bank - persistent, scope-enforced context for the fleet.

Two problems solved here.

1. Scope enforcement. Reads and writes are checked against the agent manifest
   in the Registry, so an agent physically cannot touch a collection outside
   its declared scopes even if its own code asks for it.

2. Weeks of asynchronous operation. Replaying the full history into a context
   window does not survive months of a programme. Instead the bank keeps a
   compact, structured project state plus an append-only event log, and any
   agent can rehydrate from it at any time without reading the whole past.
"""
import os, datetime
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from common import registry

PROJECT = os.environ["GCP_PROJECT"]
_db = firestore.Client(project=PROJECT)

SCOPE_COLLECTIONS = {
    "documents": "documents",
    "requirements": "requirements",
    "design": "design_elements",
    "tests": "test_cases",
    "trace_links": "trace_links",
    "findings": "findings",
    "impact_reports": "impact_reports",
}


class ScopeViolation(Exception):
    """Raised when an agent reaches outside its declared data scopes."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _authorise(agent_id, scope, mode):
    manifest = registry.get(agent_id)
    if manifest is None:
        raise ScopeViolation(f"agent '{agent_id}' is not registered")
    field = "write_scopes" if mode == "write" else "read_scopes"
    allowed = manifest.get(field, [])
    if scope not in allowed:
        raise ScopeViolation(
            f"agent '{agent_id}' may not {mode} scope '{scope}' (allowed: {allowed})")
    if scope not in SCOPE_COLLECTIONS:
        raise ScopeViolation(f"unknown scope '{scope}'")
    return SCOPE_COLLECTIONS[scope]


def write(agent_id, scope, doc_id, data, project=None):
    col = _authorise(agent_id, scope, "write")
    payload = dict(data)
    payload.update({"_agent": agent_id, "_scope": scope, "_updated": _now()})
    if project:
        payload["project"] = project
    _db.collection(col).document(doc_id).set(payload, merge=True)
    return doc_id


def read(agent_id, scope, project=None, limit=1000):
    col = _authorise(agent_id, scope, "read")
    q = _db.collection(col)
    if project:
        q = q.where(filter=FieldFilter("project", "==", project))
    return [dict(d.to_dict(), _id=d.id) for d in q.limit(limit).stream()]


def append_event(project, agent_id, kind, payload=None):
    """Append-only log. This is what makes the fleet resumable after weeks."""
    doc = _db.collection("project_events").document()
    doc.set({"project": project, "agent": agent_id, "kind": kind,
             "payload": payload or {}, "ts": _now()})
    return doc.id


def rehydrate(agent_id, project):
    """Compact state an agent resumes from, instead of replaying the history.

    Only the scopes the caller is allowed to read are included, so the same
    call returns a different, narrower picture for the auditor than for the
    linker. Scope enforcement survives even the convenience helpers.
    """
    state = {"project": project, "as_of": _now(), "visible_scopes": []}
    for scope in ("requirements", "trace_links", "findings"):
        try:
            items = read(agent_id, scope, project)
        except ScopeViolation:
            continue
        state["visible_scopes"].append(scope)
        summary = {"count": len(items)}
        if scope == "requirements":
            summary["unverifiable"] = sum(1 for r in items if r.get("verifiable") is False)
            summary["ids"] = sorted(r.get("id", "?") for r in items)[:50]
        if scope == "findings":
            summary["open"] = sum(1 for f in items if f.get("status", "open") == "open")
            by_kind = {}
            for f in items:
                k = f.get("kind", "unknown")
                by_kind[k] = by_kind.get(k, 0) + 1
            summary["by_kind"] = by_kind
        state[scope] = summary
    events = [d.to_dict() for d in _db.collection("project_events")
              .where(filter=FieldFilter("project", "==", project)).limit(500).stream()]
    events.sort(key=lambda e: e.get("ts", ""))
    state["events_total"] = len(events)
    state["recent_events"] = events[-10:]
    return state
