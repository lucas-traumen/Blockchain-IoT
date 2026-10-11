# Install dependencies before copying application source so subsequent builds
# retain the same pinned libraries when only backend code changes.
FROM python:3.12-slim-bookworm AS runtime_dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 hrc \
    && useradd --uid 10001 --gid hrc --no-create-home hrc

# Runtime without TensorFlow remains available for tests and tooling.
FROM runtime_dependencies AS iot_backend
COPY iot_backend/ iot_backend/
COPY ai_model/ ai_model/
COPY scripts/ scripts/
COPY configs/ configs/
COPY schemas/ schemas/
RUN mkdir -p data ai_model/artifacts \
    && chown -R hrc:hrc data ai_model/artifacts
USER hrc
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "iot_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime_dependencies AS ai_dependencies
# Optional package cache; the full pinned AI requirements are always installed.
ARG PIP_FIND_LINKS
ARG PIP_TRUSTED_HOST
ARG PIP_NO_INDEX
RUN pip install --no-cache-dir -r requirements-ai.txt

# Compose uses this complete AI/runtime target by default.
FROM ai_dependencies AS ai
COPY --from=iot_backend /app/ /app/
USER hrc
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "iot_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
