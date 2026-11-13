# 001 — Monólito modular, uma imagem Docker, três processos

**Status:** aceita

## Contexto

O stack foi dado: Python 3.12, FastAPI, Redis Streams, PostgreSQL, Redis, worker assíncrono,
Docker Compose, GitHub Actions. Faltava decidir como organizar o código e o deploy.

## Decisão

Um único pacote Python (`src/fraud`) com módulos de fronteira clara (`api`, `worker`,
`features`, `ml`, `simulation`, `db`, `observability`) e **uma imagem Docker** que roda como
API, worker, treino ou painel conforme o comando.

- Gerenciamento de dependências com **uv** e lockfile versionado (`uv.lock`): o build da
  imagem e o CI instalam exatamente as mesmas versões.
- `features` e `domain` não importam nada de web nem de banco. É isso que permite a mesma
  função de features no treino e no worker ([ADR 005](005-paridade-treino-servico.md)).
- A API não carrega o modelo: só lê os metadados. Quem infere é o worker.

## Alternativas descartadas

- **Um repositório/imagem por serviço.** API e worker compartilham modelos de banco,
  configuração, features e observabilidade. Separar obrigaria a publicar uma biblioteca
  interna ou duplicar código, sem nenhum ganho de escala que este projeto consiga usar.
- **Poetry ou pip-tools.** Funcionam; o uv resolve e instala em segundos e deixa a camada de
  dependências do Docker barata de reconstruir.
- **Imagem separada para treino.** Garantiria uma imagem de serviço menor (sem pandas e
  scikit-learn completos), mas o worker precisa de scikit-learn e LightGBM de qualquer
  forma para desserializar o modelo.

## Consequências

- Um `docker compose build` constrói tudo; API e worker nunca ficam em versões diferentes.
- A imagem carrega dependências que a API não usa (LightGBM, Streamlit). Aceitável aqui;
  em produção eu separaria por *extras* e estágios de build.
- O teto de `sqlalchemy<2.1` existe porque a instrumentação OpenTelemetry do SQLAlchemy
  ainda não cobre a 2.1 (ela se recusa a instrumentar e o tracing do banco some em
  silêncio). Foi descoberto no primeiro teste de fumaça.
