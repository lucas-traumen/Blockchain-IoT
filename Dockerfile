# Compose mặc định dùng target ai để runtime nạp LSTM real; target iot_backend
# vẫn tồn tại cho các test/runtime không cần TensorFlow.
FROM python:3.12-slim-bookworm AS iot_backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 hrc \
    && useradd --uid 10001 --gid hrc --no-create-home hrc
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

FROM iot_backend AS ai
USER root
RUN pip install --no-cache-dir -r requirements-ai.txt
USER hrc
