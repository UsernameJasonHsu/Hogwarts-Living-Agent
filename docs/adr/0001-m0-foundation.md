# ADR 0001: M0 foundation

Status: accepted, 2026-10-23.

Use a synchronous FastAPI/SQLAlchemy modular monolith and PostgreSQL 16.
SQLite is a fast, file-backed test alternative, not evidence of PostgreSQL locking.
Alembic revision 0001 establishes the chain; M1 will add a frozen schema revision.
The application never creates tables implicitly on startup.

Keep future web and scheduler work as documentation-only skeletons. Redis is not
required for M1: durable database run records are the queue and authority.

Follow SQLAlchemy's documented distinction between ORM version counters and bulk
updates: concurrency-critical mutations must explicitly compare expected version
in the SQL UPDATE and check its row count.

References: https://docs.sqlalchemy.org/en/20/orm/versioning.html and
https://alembic.sqlalchemy.org/en/latest/tutorial.html.
