# AGENTS.md — Student Course Registration System

This file serves as the operational guide and system instruction for AI coding agents (and developers) working inside this repository.

---

## 1. Project Overview & Context

- **Project**: Next-Gen Microservices Student Course Registration Platform
- **Course**: 13016228 Software Design and Architecture (KMITL)
- **Monorepo Layout**: 4 domain microservices + 1 API Gateway + shared library
- **Primary Goal**: High-concurrency course registration ensuring zero overbooking, instantaneous validation, and decoupled service boundaries.
- **Academic Defense Focus**: Strict, idiomatic implementation of 5 Gang of Four (GoF) design patterns.

---

## 2. Microservice Registry & Architecture

The system strictly adheres to a **shared-nothing database** pattern. Services communicate exclusively via async HTTP (`httpx.AsyncClient`) or event payloads.

| Service | Port | Database | Primary Responsibility | Architectural Role |
|---|---|---|---|---|
| **API Gateway** | `8000` | Stateless | Ingress reverse proxy, JWT authentication (`/auth/login`), token-bucket rate limiter. | **Protection Proxy Pattern** |
| **Student Service** | `8001` | `student.db` | Student profiles, academic standing, GPA, financial holds, credential verification. | Domain Service |
| **Catalog Service** | `8002` | `catalog.db` | Course offerings, sections, quotas, prerequisite Directed Acyclic Graph (DAG) validation. | Domain Service |
| **Enrollment Service** | `8003` | `enrollment.db` | Core registration engine, atomic seat allocation, rule validation, waitlist management. | **Mediator, Strategy, State Patterns** |
| **Notification Service** | `8004` | Append-Only Log | Asynchronous subscriber for enrollment confirmations and waitlist offer alerts. | **Observer Pattern** |

Shared module:
- `common/`: Shared Pydantic DTOs (`schemas.py`), JWT security utilities (`auth.py`), and event schemas (`events.py`).

---

## 3. Mandatory Design Pattern Mapping

Agents **must preserve and respect** the following design patterns during code changes and extensions:

1. **Strategy Pattern (`enrollment_service/validation.py`)**
   - Pluggable, interchangeable validation rules inheriting from an abstract base class `ValidationStrategy` (e.g., `PrerequisiteRule`, `CreditLimitRule`, `TimetableConflictRule`, `FinancialHoldRule`).
   - The validation engine executes strategies sequentially or in parallel and short-circuits or aggregates violations.

2. **Mediator Pattern (`enrollment_service/orchestrator.py`)**
   - The `EnrollmentOrchestrator` mediates cross-service communication between Student Service (:8001), Catalog Service (:8002), and Notification Service (:8004).
   - Domain services never directly call each other; all orchestration flows through the Mediator.

3. **State Pattern (`enrollment_service/state_machine.py`)**
   - Encapsulates student registration state transitions:  
     `STAGED -> OFFERED -> ENROLLED -> DROPPED` / `WAITLISTED`.
   - State-specific logic and transition guards are encapsulated inside concrete state classes.

4. **Protection Proxy Pattern (`api_gateway/rate_limiter.py` & `api_gateway/auth.py`)**
   - Protects downstream microservices from burst spam and unauthorized access.
   - Authenticates JWT tokens and applies token-bucket rate limiting before forwarding requests.
   - Injects verified `X-Student-Id` header into proxied requests.

5. **Observer Pattern (`notification_service/event_consumer.py`)**
   - Subscribers consume event topics (e.g., `EnrollmentConfirmedEvent`, `WaitlistSlotOfferedEvent`) dispatched asynchronously without coupling the enrollment transaction to alert delivery.

---

## 4. Architectural Rules & Invariants

When generating or editing code in this repository, strictly adhere to these invariants:

1. **Shared-Nothing Databases**:
   - Each service strictly manages its own SQLite file (`student.db`, `catalog.db`, `enrollment.db`).
   - **NEVER** write cross-database joins, direct file reads of other service databases, or import models/sessions across service directories.

2. **Zero Overbooking Invariant (Atomic SQL Updates)**:
   - In `enrollment_service/seat_allocator.py`, seat reservations **must not** use naive read-modify-write patterns (`if enrolled < capacity: enrolled += 1`).
   - **Must use** atomic conditional SQL updates:
     ```sql
     UPDATE sections 
     SET enrolled = enrolled + 1 
     WHERE id = :section_id AND enrolled < capacity;
     ```
   - Check rows affected (`cursor.rowcount == 1`). If `0`, capacity is exhausted; divert to waitlist.

3. **Header Propagation & Security**:
   - API Gateway verifies the JWT bearer token, extracts `student_id`, and injects the header `X-Student-Id: <id>`.
   - Downstream services read `X-Student-Id` directly from request headers.

4. **Async-First Execution**:
   - All FastAPI endpoints must be `async def`.
   - All database queries must use SQLAlchemy 2.0 Async (`AsyncSession`, `await session.execute(...)`).
   - All inter-service HTTP requests must use non-blocking `httpx.AsyncClient` with proper timeouts.

5. **Pydantic v2 Conventions**:
   - Always use Pydantic v2 syntax: `from pydantic import BaseModel, ConfigDict`, `model_dump()`, `model_validate()`. Avoid deprecated v1 methods like `.dict()`.

---

## 5. Toolchain & Command Reference

The project exclusively uses **Astral `uv`** for dependency management, virtual environments, and task execution. **Do not use `pip`, `pipenv`, or `poetry`.**

### Environment & Dependencies
```powershell
# Sync virtualenv from uv.lock
uv sync
```

### Running Tests
```powershell
# Run entire test suite
uv run pytest -v

# Run a specific test file
uv run pytest tests/test_catalog_api.py -v

# Run with output captured/printed
uv run pytest -s
```

### Linting & Formatting
```powershell
# Run Ruff linting
uv run ruff check .

# Automatically fix linting and style errors
uv run ruff check --fix .
```

### Running Services Locally
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

# Load Test Bench Web UI (:8080)
uv run uvicorn load_tester.main:app --port 8080 --reload
```

---

## 6. Agent Workflow & Quality Checklist

Before finalizing any task or proposing changes:
1. **Preserve Comments & Contracts**: Keep existing route docstrings and type annotations intact.
2. **Lint Cleanliness**: Execute `uv run ruff check .` to guarantee zero lint violations.
3. **Automated Verification**: Run `uv run pytest` to ensure no regression in service health checks or domain logic.
4. **Service Scope Respect**: Keep changes scoped to the specific service being worked on (`api_gateway`, `student_service`, `catalog_service`, `enrollment_service`, or `notification_service`).
