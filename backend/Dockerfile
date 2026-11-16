FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# R-7：非 root 运行；/app 与 /data 归 appuser 所有
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser \
    && mkdir -p /data && chown -R appuser:appuser /app /data

EXPOSE 8000

USER appuser

# SQLite 下保持单 worker，避免写锁冲突
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
