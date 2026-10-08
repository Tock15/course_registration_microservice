"""
catalog_service/schemas.py
Service-specific Pydantic schemas for Course Catalog operations.
"""
from pydantic import BaseModel, Field


class CourseCreateRequest(BaseModel):
    code: str = Field(..., min_length=2, max_length=20, description="e.g. CS101")
    title: str = Field(..., min_length=2, max_length=150)
    description: str | None = None
    credits: int = Field(default=3, gt=0, le=6)
    department: str = Field(default="Computer Engineering", max_length=100)
    is_elective: bool = False
    prerequisites: list[str] = Field(
        default_factory=list, description="Course codes required prior to taking this course"
    )


class SectionCreateRequest(BaseModel):
    course_code: str = Field(..., description="Target course code, e.g. CS101")
    section_number: int = Field(default=1, gt=0)
    capacity: int = Field(default=40, gt=0)
    semester: str = Field(default="2026/1")
    academic_year: int = Field(default=2026, ge=2020)
    room: str = Field(default="TBA")
    schedule_days: list[str] = Field(default_factory=lambda: ["MON", "WED"])
    start_time: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(default="10:30", pattern=r"^\d{2}:\d{2}$")
    instructor: str = Field(default="TBA")


class CapacityUpdateRequest(BaseModel):
    capacity: int = Field(..., gt=0, description="Updated maximum seat quota")
