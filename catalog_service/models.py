"""
catalog_service/models.py
SQLAlchemy 2.0 ORM models for Course, Prerequisite, and Section entities.
"""
import json
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Course(Base):
    __tablename__ = "courses"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    department: Mapped[str] = mapped_column(
        String(100), nullable=False, default="Computer Engineering"
    )
    is_elective: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    prerequisite_records: Mapped[list["Prerequisite"]] = relationship(
        "Prerequisite",
        foreign_keys="[Prerequisite.course_code]",
        back_populates="course",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    sections: Mapped[list["Section"]] = relationship(
        "Section",
        back_populates="course",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def prerequisites(self) -> list[str]:
        """Expose required course codes as list of strings for Pydantic CourseDTO validation."""
        return [p.required_course_code for p in self.prerequisite_records]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "description": self.description,
            "credits": self.credits,
            "department": self.department,
            "is_elective": self.is_elective,
            "prerequisites": self.prerequisites,
        }


class Prerequisite(Base):
    __tablename__ = "prerequisites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_code: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("courses.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    required_course_code: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("courses.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    min_grade_required: Mapped[str] = mapped_column(String(5), default="D")

    # Relationship
    course: Mapped["Course"] = relationship(
        "Course",
        foreign_keys=[course_code],
        back_populates="prerequisite_records",
    )


class Section(Base):
    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_code: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("courses.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    semester: Mapped[str] = mapped_column(String(20), default="2026/1")
    academic_year: Mapped[int] = mapped_column(Integer, default=2026)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    enrolled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    room: Mapped[str] = mapped_column(String(50), default="TBA")
    schedule_days_raw: Mapped[str] = mapped_column(
        "schedule_days", String(100), default="[]"
    )
    start_time: Mapped[str] = mapped_column(String(10), default="09:00")
    end_time: Mapped[str] = mapped_column(String(10), default="10:30")
    instructor: Mapped[str] = mapped_column(String(100), default="TBA")

    # Relationship
    course: Mapped["Course"] = relationship("Course", back_populates="sections")

    @property
    def schedule_days(self) -> list[str]:
        """Expose days as a parsed list of strings for Pydantic SectionDTO validation."""
        if not self.schedule_days_raw:
            return []
        try:
            parsed = json.loads(self.schedule_days_raw)
            if isinstance(parsed, list):
                return [str(d) for d in parsed]
        except (json.JSONDecodeError, TypeError):
            return [d.strip() for d in self.schedule_days_raw.split(",") if d.strip()]
        return [d.strip() for d in self.schedule_days_raw.split(",") if d.strip()]

    @schedule_days.setter
    def schedule_days(self, days: list[str] | str) -> None:
        if isinstance(days, list):
            self.schedule_days_raw = json.dumps(days)
        else:
            self.schedule_days_raw = str(days)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_code": self.course_code,
            "section_number": self.section_number,
            "capacity": self.capacity,
            "enrolled_count": self.enrolled_count,
            "schedule_days": self.schedule_days,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "room": self.room,
            "instructor": self.instructor,
        }
