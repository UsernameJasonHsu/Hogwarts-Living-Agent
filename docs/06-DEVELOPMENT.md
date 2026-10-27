# M0/M1 development

Python 3.12+, PostgreSQL 16, SQLAlchemy 2.0, Alembic, Pydantic 2, FastAPI.
Run commands from the repository root. `requirements.lock` records the versions
actually used for verification; it includes development tools.

## Local setup (PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
$env:DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@localhost:5432/hogwarts'
$env:HUMAN_API_TOKEN = '<generate-a-random-token-at-least-32-characters>'
$env:WORKER_API_TOKEN = '<a-different-random-token-at-least-32-characters>'
alembic upgrade head
uvicorn hogwarts.entrypoints.bootstrap:create_formal_app --factory --app-dir apps/api
```

Execution defaults to `HOGWARTS_EXECUTION_MODE=disabled`. For the pure M1 fake
workflow, explicitly set it to `fake`; only the exact known fake adapters are
accepted. Operational execution requires trusted operator code to construct a
`FormalExecutionGateway` and pass it to `create_formal_app(mode="operational",
gateway=...)`. Setting the environment variable alone fails startup. The gateway
binds approved profiles, complete private storage roots, grants and durable quota
configuration; Agent requests cannot supply adapters or deployment factories.
See [formal entrypoints](27-FORMAL-ENTRYPOINTS.md). `main.create_app` with injected
adapters is a trusted compatibility/test embedding API, not an approved deployment
entrypoint. Never run Agent Python inside the trusted Controller.

Secrets are environment-only. Configure distinct ASCII human/worker tokens, each
at least 32 characters. Non-ASCII configuration fails closed with 503; invalid
non-ASCII incoming credentials return 401. Do not commit `.env` or token values. URL-encode
passwords when building PostgreSQL connection URLs. No default usable token exists.
The API is intended for localhost development, with one configured owner.

## Docker Compose

Set `POSTGRES_PASSWORD`, `HUMAN_API_TOKEN` and `WORKER_API_TOKEN` in the shell,
then `docker compose up --build`. API binds to `127.0.0.1:8000`.
Database is private to the Compose network; migrations run before the API starts.
`docker compose down` retains the database volume. No real execution worker starts.

## Checks and migrations

```powershell
ruff check .
ruff format --check .
pytest -v --junitxml=test-results/junit.xml
# Optional PostgreSQL integration suite; use a DEDICATED disposable test database.
$env:TEST_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@localhost:5432/hogwarts_test'
pytest -v --junitxml=test-results/junit.xml
# Rollback only against a disposable test DB; downgrade deletes milestone tables.
alembic downgrade base
alembic upgrade head
alembic check
```

The foundation suite runs SQLite migration up/down/up. PostgreSQL tests use an
isolated schema per test and drop that schema afterward, never application tables.
CI provisions PostgreSQL 16 and runs both backends. `/docs` and `/openapi.json`
describe implemented endpoints. Future world and real runtime endpoints are absent.

## Workspace boundaries

`apps/web`, `packages`, and the scheduler are placeholders. `contracts/` retains
the original v1 document contracts; HTTP command schemas are separately versioned
through `/api/v1` and exported OpenAPI. M1/M2 deterministic components are present;
M3–M5 and real Codex integration remain outside this milestone.
