"""One place where the model is called, so failure behaves the same everywhere.

Vertex AI answers 429 RESOURCE_EXHAUSTED under quota pressure. Unhandled it
surfaces as a 500 and the system looks broken when it is merely throttled -
which is exactly how a reviewer would experience it.

So: bounded retries with exponential backoff and jitter, and a distinct
exception so callers can degrade honestly instead of pretending the model
returned something.
"""
import os, time, random
from google import genai

PROJECT = os.environ["GCP_PROJECT"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
_client = genai.Client(vertexai=True, project=PROJECT, location="global")

MAX_ATTEMPTS = 4
RETRYABLE_MARKERS = ("RESOURCE_EXHAUSTED", "429", "UNAVAILABLE", "503",
                     "DEADLINE_EXCEEDED", "504", "INTERNAL", "500")


class ModelUnavailable(Exception):
    """The model could not be reached. An infrastructure fact, not a bad answer."""


def _is_retryable(exc):
    blob = f"{getattr(exc, 'code', '')} {getattr(exc, 'status', '')} {exc}".upper()
    return any(m in blob for m in RETRYABLE_MARKERS)


def generate(prompt, model=None):
    """Call Gemini, retrying transient failures. Raises ModelUnavailable if not."""
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _client.models.generate_content(
                model=model or MODEL, contents=prompt).text
        except Exception as exc:
            last = exc
            if not _is_retryable(exc) or attempt == MAX_ATTEMPTS:
                break
            # 2s, 4s, 8s plus jitter, so parallel agents do not retry in lockstep
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 1.5))
    raise ModelUnavailable(
        f"Gemini unavailable after {MAX_ATTEMPTS} attempts: "
        f"{type(last).__name__}: {last}"[:300])
