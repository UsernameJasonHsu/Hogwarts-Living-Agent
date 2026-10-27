FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
ENV PYTHONPATH=/app/apps/api
RUN useradd --create-home appuser
USER appuser
CMD ["uvicorn", "hogwarts.entrypoints.bootstrap:create_formal_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
