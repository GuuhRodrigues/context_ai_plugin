FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project

COPY . .

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "core.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
