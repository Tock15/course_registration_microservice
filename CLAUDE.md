# CLAUDE.md

> **Primary Instructions**: See [AGENTS.md](AGENTS.md) for full architectural guidelines, service boundaries, Gang of Four (GoF) design pattern mappings, and invariants.

---

## Quick Reference & Commands

This repository strictly uses **Astral `uv`**. Do not use `pip`, `pipenv`, or `poetry`.

### Dependencies & Setup
```powershell
uv sync
```

### Testing
```powershell
# Run all tests
uv run pytest -v

# Run specific test file
uv run pytest tests/test_catalog_api.py -v
```

### Linting & Formatting
```powershell
# Lint check
uv run ruff check .

# Fix lint & formatting issues
uv run ruff check --fix .
```

### Running Services
```powershell
# API Gateway (:8000)
uv run uvicorn api_gateway.main:app --port 8000 --reload

# Student Service (:8001)
uv run uvicorn student_service.main:app --port 8001 --reload

# Catalog Service (:8002)
uv run uvicorn catalog_service.main:app --port 8002 --reload

# Enrollment Service (:8003)
uv run uvicorn enrollment_service.main:app --port 8003 --reload

# Notification Service (:8004)
uv run uvicorn notification_service.main:app --port 8004 --reload
```

---

## Core Guidelines & Invariants

1. **Detailed Architecture & Rules**: Read [AGENTS.md](AGENTS.md).
2. **Shared-Nothing Databases**: Never write cross-database queries or import models across service boundaries (`student.db`, `catalog.db`, `enrollment.db`).
3. **Zero Overbooking**: Atomic conditional SQL updates in `enrollment_service/seat_allocator.py` only (`rowcount == 1`).
4. **Design Patterns**: Preserve Strategy (Validation), Mediator (Orchestration), State (Enrollment Lifecycle), Protection Proxy (Gateway), and Observer (Notifications).
5. **Async-First & Pydantic v2**: All endpoints and DB calls must be async; strictly use Pydantic v2 syntax.
