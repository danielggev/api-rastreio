FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# Migrations e utilitarios operacionais. Nao entram no pacote Python, mas sao
# executados de dentro do container:
#   alembic upgrade head        -> precisa de alembic.ini e alembic/
#   python scripts/expurgar.py  -> rodado pelo cron do expurgo LGPD
COPY alembic ./alembic
COPY scripts ./scripts

# Usuario sem privilegios: um comprometimento da aplicacao nao vira root no
# container.
RUN useradd --create-home --uid 1000 rastreio && chown -R rastreio:rastreio /app
USER rastreio

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
