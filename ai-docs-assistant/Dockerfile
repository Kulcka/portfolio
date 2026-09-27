# Образ Telegram-бота. Документы и индекс монтируются томами, ключи — через --env-file.
#   docker build -t ai-docs-assistant .
#   docker run -d --name docs-bot --restart unless-stopped --env-file .env \
#     -v "$PWD/docs:/app/docs:ro" -v docs-index:/app/data ai-docs-assistant
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DOCS_DIR=/app/docs \
    INDEX_DIR=/app/data/index

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY docs_assistant ./docs_assistant
COPY demo_docs ./docs

RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown -R bot /app/data
USER bot

CMD ["python", "-m", "docs_assistant", "bot"]
