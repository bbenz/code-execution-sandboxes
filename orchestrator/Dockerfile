# Orchestrator for the referee race. A normal Azure Container App; it drives sandboxes through the data plane.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

COPY orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestrator/ .
# The orchestrator reads the target metadata to show each project's issue and editable files.
COPY engine/targets ./targets

EXPOSE 8000
CMD ["sh", "-c", "uvicorn orchestrator:app --host 0.0.0.0 --port ${PORT} --workers 1"]
