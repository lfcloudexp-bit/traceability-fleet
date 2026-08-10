import os, json, uuid, datetime
from fastapi import FastAPI
from pydantic import BaseModel
from google import genai
from google.cloud import firestore

PROJECT = os.environ["GCP_PROJECT"]
MODEL   = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

app = FastAPI(title="Traceability Fleet - Gateway")
gclient = genai.Client(vertexai=True, project=PROJECT, location="global")
db = firestore.Client(project=PROJECT)

class Spec(BaseModel):
    text: str
    source: str = "manual"

@app.get("/")
def health():
    return {"status": "ok", "model": MODEL, "project": PROJECT}

@app.post("/ingest")
def ingest(spec: Spec):
    prompt = (
        "You are a requirements engineer. Extract ATOMIC requirements from the "
        "specification text below. Answer ONLY with a JSON array. Each element: "
        '{"id": "REQ-###", "text": "...", "type": "functional|non-functional", '
        '"verifiable": true|false}. Always answer in English.\n\n'
        f"SPECIFICATION:\n{spec.text}"
    )
    raw = gclient.models.generate_content(model=MODEL, contents=prompt).text
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        reqs = json.loads(cleaned)
    except Exception:
        return {"error": "model did not return valid JSON", "raw": raw[:500]}

    batch_id = str(uuid.uuid4())[:8]
    for r in reqs:
        db.collection("requirements").document(f"{batch_id}-{r.get('id','?')}").set(
            {**r, "batch": batch_id, "source": spec.source,
             "ingested_at": datetime.datetime.utcnow().isoformat()}
        )
    return {"batch": batch_id, "extracted": len(reqs), "requirements": reqs}
