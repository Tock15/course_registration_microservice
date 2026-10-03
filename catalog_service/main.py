"""
catalog_service/main.py
Course Catalog & Prerequisite DAG Service (Port 8002).

Responsibilities for Teammate:
  - Course & Section models (models.py) & async SQLite connection (database.py)
  - Course catalog exploration: GET /courses, GET /sections
  - Prerequisite Directed Acyclic Graph (DAG) engine: prereq_dag.py
  - Prerequisite validation endpoint: POST /validate-prereqs
"""
from fastapi import FastAPI

app = FastAPI(
    title="Course Catalog Service",
    description="Manages course offerings, sections, quotas, and prerequisite graph traversal.",
    version="0.1.0",
)


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint for service monitoring and API Gateway routing."""
    return {
        "service": "catalog_service",
        "status": "healthy",
        "port": 8002,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": "Course Catalog Service",
        "version": "0.1.0",
        "docs_url": "/docs",
    }
