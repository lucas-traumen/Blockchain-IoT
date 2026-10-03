# Compose selects backend or ai explicitly; RF builds stop before TensorFlow.
FROM python:3.12-slim-bookworm AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 hrc \
    && useradd --uid 10001 --gid hrc --no-create-home hrc
COPY backend/ backend/
COPY ai_model/ ai_model/
COPY scripts/ scripts/
COPY configs/ configs/
COPY schemas/ schemas/
RUN mkdir -p data ai_model/artifacts \
    && chown -R hrc:hrc data ai_model/artifacts
USER hrc
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM backend AS ai
USER root
RUN pip install --no-cache-dir -r requirements-ai.txt
USER hrc
