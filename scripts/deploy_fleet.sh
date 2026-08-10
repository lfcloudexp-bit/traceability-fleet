#!/usr/bin/env bash
# Deploy the fleet: one image, four identities, four private services.
set -uo pipefail
PID=project-b2ac433a-5081-43cd-8c0
NUM=361142549445
REGION=europe-west1
TOPIC=agent-tasks
AGENTS="ingestor linker auditor impact"
IMAGE=$REGION-docker.pkg.dev/$PID/tf/fleet:v1

echo "== 1. identidades por agente =="
for A in $AGENTS; do
  gcloud iam service-accounts create agent-$A --display-name "Fleet agent: $A" >/dev/null 2>&1
  SA=agent-$A@$PID.iam.gserviceaccount.com
  for ROLE in roles/aiplatform.user roles/datastore.user roles/cloudtrace.agent; do
    gcloud projects add-iam-policy-binding $PID --member="serviceAccount:$SA" \
      --role=$ROLE --quiet >/dev/null 2>&1
  done
  echo "   $SA"
done

echo "== 2. identidad de entrega de Pub/Sub =="
gcloud iam service-accounts create pubsub-invoker \
  --display-name "Pub/Sub push invoker" >/dev/null 2>&1
INVOKER=pubsub-invoker@$PID.iam.gserviceaccount.com
gcloud iam service-accounts add-iam-policy-binding $INVOKER \
  --member="serviceAccount:service-$NUM@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role=roles/iam.serviceAccountTokenCreator --quiet >/dev/null 2>&1
echo "   $INVOKER"

echo "== 3. imagen unica =="
gcloud artifacts repositories create tf --repository-format=docker \
  --location=$REGION --quiet >/dev/null 2>&1
gcloud builds submit --tag $IMAGE --quiet > /tmp/build.log 2>&1 \
  && echo "   OK $IMAGE" || { echo "   FALLO build"; tail -15 /tmp/build.log; exit 1; }

echo "== 4. cuatro servicios privados, una identidad cada uno =="
for A in $AGENTS; do
  gcloud run deploy tf-agent-$A --image $IMAGE --region $REGION \
    --no-allow-unauthenticated \
    --service-account agent-$A@$PID.iam.gserviceaccount.com \
    --set-env-vars "GCP_PROJECT=$PID,GEMINI_MODEL=gemini-3.5-flash,AGENT_ID=$A,SERVICE_MODULE=services.agent.main" \
    --quiet > /tmp/dep-$A.log 2>&1 \
    && echo "   OK tf-agent-$A" \
    || { echo "   FALLO tf-agent-$A"; tail -4 /tmp/dep-$A.log; }
done

echo "== 5. suscripciones push filtradas por agente =="
for A in $AGENTS; do
  URL=$(gcloud run services describe tf-agent-$A --region $REGION \
        --format='value(status.url)' 2>/dev/null)
  if [ -z "$URL" ]; then echo "   sin URL para $A"; continue; fi
  gcloud run services add-iam-policy-binding tf-agent-$A --region $REGION \
    --member="serviceAccount:$INVOKER" --role=roles/run.invoker --quiet >/dev/null 2>&1
  gcloud pubsub subscriptions create sub-$A --topic $TOPIC \
    --push-endpoint="$URL/pubsub" \
    --push-auth-service-account=$INVOKER \
    --message-filter="attributes.agent = \"$A\"" \
    --ack-deadline=600 --quiet >/dev/null 2>&1 \
    && echo "   sub-$A -> $URL" || echo "   sub-$A ya existia o fallo"
done
echo "== FIN =="
