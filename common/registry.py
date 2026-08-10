"""Agent Registry - discovery, versioning and governance of the fleet.

Every agent is declared here before it may run. The manifest is the single
source of truth for what an agent may touch, and the Gateway calls authorize()
on every hop. An agent that is not registered cannot act.
"""
import os, datetime
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

PROJECT = os.environ["GCP_PROJECT"]
_db = firestore.Client(project=PROJECT)
COL = "agent_registry"

FLEET = [
    {"agent_id": "ingestor", "version": "1.0.0",
     "department": "Systems Engineering",
     "description": "Extracts atomic, uniquely identified requirements from specifications.",
     "service_account": "agent-ingestor",
     "allowed_tools": ["gemini.generate", "firestore.write:requirements"],
     "read_scopes": ["documents"], "write_scopes": ["requirements"],
     "status": "approved"},
    {"agent_id": "linker", "version": "1.0.0",
     "department": "Systems Engineering",
     "description": "Proposes and maintains trace links requirement -> design -> test.",
     "service_account": "agent-linker",
     "allowed_tools": ["gemini.generate", "firestore.write:trace_links"],
     "read_scopes": ["requirements", "design", "tests"],
     "write_scopes": ["trace_links"], "status": "approved"},
    {"agent_id": "auditor", "version": "1.0.0",
     "department": "Quality Assurance",
     "description": "Detects orphans, contradictions and unverifiable requirements.",
     "service_account": "agent-auditor",
     "allowed_tools": ["gemini.generate", "firestore.write:findings"],
     "read_scopes": ["requirements", "trace_links"], "write_scopes": ["findings"],
     "status": "approved",
     "note": "Separation of duties: the auditor cannot modify what it audits."},
    {"agent_id": "impact", "version": "1.0.0",
     "department": "Change Management",
     "description": "Traces downstream consequences when a requirement changes.",
     "service_account": "agent-impact",
     "allowed_tools": ["gemini.generate", "firestore.write:impact_reports"],
     "read_scopes": ["requirements", "trace_links", "findings"],
     "write_scopes": ["impact_reports"], "status": "approved"},
]


def publish(manifest):
    """Publish or update an agent manifest. Versioned, never overwritten in place."""
    key = f"{manifest['agent_id']}@{manifest['version']}"
    manifest = dict(manifest)
    manifest["published_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _db.collection(COL).document(key).set(manifest)
    return key


def get(agent_id, version=None):
    """Resolve an agent. Without a version, the highest approved one wins."""
    if version:
        snap = _db.collection(COL).document(f"{agent_id}@{version}").get()
        return snap.to_dict() if snap.exists else None
    found = [d.to_dict() for d in _db.collection(COL)
             .where(filter=FieldFilter("agent_id", "==", agent_id))
             .where(filter=FieldFilter("status", "==", "approved")).stream()]
    if not found:
        return None
    return sorted(found, key=lambda m: [int(p) for p in m["version"].split(".")])[-1]


def list_agents(department=None):
    out = [d.to_dict() for d in _db.collection(COL).stream()]
    if department:
        out = [m for m in out if m.get("department") == department]
    return sorted(out, key=lambda m: m["agent_id"])


def deprecate(agent_id, version):
    _db.collection(COL).document(f"{agent_id}@{version}").update({"status": "deprecated"})


class PolicyViolation(Exception):
    """Raised when an agent attempts something its manifest does not allow."""


def authorize(agent_id, tool):
    """Zero-trust check. Called by the Gateway on EVERY hop, not once at start.

    Denies by default: an unregistered, deprecated or out-of-scope agent
    cannot act, no matter what the calling code believes.
    """
    manifest = get(agent_id)
    if manifest is None:
        raise PolicyViolation(f"agent '{agent_id}' is not registered")
    if manifest.get("status") != "approved":
        raise PolicyViolation(f"agent '{agent_id}' is {manifest.get('status')}")
    if tool not in manifest.get("allowed_tools", []):
        raise PolicyViolation(
            f"agent '{agent_id}' is not allowed to use '{tool}' "
            f"(allowed: {manifest.get('allowed_tools')})")
    return manifest


def seed():
    return [publish(m) for m in FLEET]


if __name__ == "__main__":
    print("published:", seed())
