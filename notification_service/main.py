"""
notification_service/main.py
Notification & Event Consumer Service (Port 8004).

Responsibilities for Teammate:
  - Event Consumer & Subscriber (Observer Pattern): event_consumer.py
  - Notification dispatch / audit store (models.py, database.py)
  - Endpoints: POST /events, GET /notifications/{student_id}
"""
from fastapi import FastAPI

app = FastAPI(
    title="Notification Service",
    description="Asynchronous alert service dispatching registration and waitlist notices.",
    version="0.1.0",
)


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint for service monitoring and API Gateway routing."""
    return {
        "service": "notification_service",
        "status": "healthy",
        "port": 8004,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": "Notification Service",
        "version": "0.1.0",
        "docs_url": "/docs",
    }
