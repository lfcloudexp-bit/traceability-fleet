#!/usr/bin/env bash
set -uo pipefail
PID=project-b2ac433a-5081-43cd-8c0
REGION=europe-west1
IMAGE=$REGION-docker.pkg.dev/$PID/tf/fleet:v2

echo "== 1. imagen =="
gcloud builds submit --tag $IMAGE --quiet > /tmp/b.log 2>&1 \
  && echo "   OK $IMAGE" || { echo "   FALLO build"; tail -12 /tmp/b.log; exit 1; }

echo "== 2. gateway =="
gcloud run deploy tf-gateway --image $IMAGE --region $REGION \
  --allow-unauthenticated \
  --set-env-vars "GCP_PROJECT=$PID,GEMINI_MODEL=gemini-3.5-flash,TASK_TOPIC=agent-tasks,SERVICE_MODULE=services.gateway.main" \
  --quiet > /tmp/g.log 2>&1 && echo "   OK" || { echo "   FALLO"; tail -5 /tmp/g.log; }

echo "== 3. agentes =="
for A in ingestor linker auditor impact; do
  gcloud run deploy tf-agent-$A --image $IMAGE --region $REGION \
    --no-allow-unauthenticated \
    --service-account agent-$A@$PID.iam.gserviceaccount.com \
    --set-env-vars "GCP_PROJECT=$PID,GEMINI_MODEL=gemini-3.5-flash,AGENT_ID=$A,SERVICE_MODULE=services.agent.main" \
    --quiet > /tmp/a-$A.log 2>&1 && echo "   OK $A" || { echo "   FALLO $A"; tail -4 /tmp/a-$A.log; }
done
echo "== FIN =="
