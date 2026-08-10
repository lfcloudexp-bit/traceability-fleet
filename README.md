# Traceability Fleet

A governed fleet of agents that keeps requirements traceability alive.

Submission for the **All Things Agentic Hackathon** (Google) - category
*Fortified Enterprise Fleet*. Built with Gemini 3.5, Google Cloud Run,
Firestore and Pub/Sub.

## The friction

In regulated engineering, the trace matrix linking requirement to design to
test is the artefact everyone depends on and nobody maintains. It rots quietly
between reviews: requirements get edited, tests get renamed, links go stale,
and the gap only surfaces during an audit.

## The twist

A specification document is **untrusted input**. Anyone can type text into a
requirement. An agent fleet that reads specifications therefore needs identity,
policy enforcement, inline guardrails and an audit trail - not as compliance
theatre, but because the documents themselves are an attack surface.

## Status

Work in progress. See docs/architecture.md.
