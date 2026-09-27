# Образ бота приёма заявок. Сборка и запуск:
#   docker build -t leads-bot .
#   docker run -d --name leads-bot --restart unless-stopped \
#     --env-file .env -v leads-data:/app/data leads-bot
# Для MAX с сертификатом Минцифры добавьте:
#   -v /path/russian_trusted_root_ca.pem:/app/certs/root.pem:ro -e MAX_CA_BUNDLE=/app/certs/root.pem
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/leads.sqlite3 \
    CONFIG_PATH=/app/config.yaml

WORKDIR /app
RUN useradd --create-home --uid 10001 bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY leads_bot ./leads_bot
COPY config.yaml .
RUN mkdir -p /app/data && chown bot:bot /app/data

USER bot
VOLUME ["/app/data"]
# Порт нужен только для MAX_MODE=webhook (MAX_WEBHOOK_HOST=0.0.0.0)
EXPOSE 8080
CMD ["python", "-m", "leads_bot", "run"]
