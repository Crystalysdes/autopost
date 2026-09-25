FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/app/data/autopost.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot

# Бот работает не от root; база лежит в томе /app/data
RUN useradd --create-home --uid 1000 botuser \
    && mkdir -p /app/data \
    && chown botuser:botuser /app/data
USER botuser
VOLUME ["/app/data"]

CMD ["python", "-m", "bot"]
