FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# One image, many roles. The module and the identity are injected per service,
# so every agent runs the same audited artefact under its own service account.
ENV SERVICE_MODULE=services.gateway.main
ENV PORT=8080
CMD exec uvicorn $SERVICE_MODULE:app --host 0.0.0.0 --port $PORT
