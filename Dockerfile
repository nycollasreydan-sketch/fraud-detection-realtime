# Uma imagem para todos os processos (API, worker, treino, painel): ADR 001.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# libgomp1: runtime OpenMP exigido pelo LightGBM
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.8 \
    && useradd --create-home --uid 1000 app

WORKDIR /app

# Dependências primeiro, e só com os dois arquivos que as definem: a camada pesada (~2 GB)
# só é refeita quando o lockfile muda. O README fica para depois de propósito; copiado aqui,
# qualquer edição de documentação invalidava o cache e custava minutos de rebuild.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra dashboard --no-install-project

COPY README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv sync --frozen --no-dev --extra dashboard \
    && mkdir -p /app/artifacts && chown -R app:app /app/artifacts

USER app
EXPOSE 8000 8501 9100
CMD ["uvicorn", "fraud.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
