FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
ENV PYTHONPATH=/app/apps/api
RUN useradd --create-home appuser
USER appuser
CMD ["uvicorn", "hogwarts.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
