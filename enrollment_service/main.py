"""
enrollment_service/main.py
Enrollment Core Engine & Orchestrator (Port 8003).

Responsibilities for Teammate:
  - Local ACID registration boundary: enrollment.db (database.py, models.py)
  - Service Orchestrator (Mediator Pattern): orchestrator.py
  - Pluggable Rule Validation (Strategy Pattern): validation.py
  - Atomic Zero-Overbooking Seat Allocation: seat_allocator.py
  - Registration Lifecycle (State Pattern): state_machine.py
  - Waitlist Management & Expiry: waitlist.py
  - Endpoints: POST /enrollments, POST /drops, POST /waitlist/claim
"""
from fastapi import FastAPI

app = FastAPI(
    title="Enrollment Service",
    description="Core registration engine featuring atomic seat allocation, rule validation, and waitlists.",
    version="0.1.0",
)


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint for service monitoring and API Gateway routing."""
    return {
        "service": "enrollment_service",
        "status": "healthy",
        "port": 8003,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": "Enrollment Engine Service",
        "version": "0.1.0",
        "docs_url": "/docs",
    }
