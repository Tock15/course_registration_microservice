"""
student_service/main.py
Student Profile & Academic Standing Service (Port 8001).

Responsibilities for Teammate:
  - Student database models (models.py) & async SQLite connection (database.py)
  - Profile retrieval: GET /students/{id}
  - Academic eligibility verification: GET /students/{id}/eligibility
  - Credential verification for login: POST /students/verify-credentials
"""
from fastapi import FastAPI

app = FastAPI(
    title="Student Profile Service",
    description="Manages student profiles, GPA, academic standing, and eligibility.",
    version="0.1.0",
)


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint for service monitoring and API Gateway routing."""
    return {
        "service": "student_service",
        "status": "healthy",
        "port": 8001,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": "Student Profile Service",
        "version": "0.1.0",
        "docs_url": "/docs",
    }
