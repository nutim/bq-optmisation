FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY sql ./sql
RUN pip install --no-cache-dir .

USER 65532:65532
CMD ["sh", "-c", "python -m uvicorn app.api:api --host 0.0.0.0 --port ${PORT}"]
