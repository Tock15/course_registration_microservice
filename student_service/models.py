"""
student_service/models.py
SQLAlchemy 2.0 ORM models and password hashing for Student Profile entities.
"""
import hashlib
import hmac
import json
import secrets
from typing import Any

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.schemas import AcademicStanding, StudentDTO


def hash_password(password: str, salt: str | None = None) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with 100,000 iterations."""
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000,
    )
    return f"{salt}${key.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain password against PBKDF2 salt$hash format."""
    try:
        salt, key_hex = hashed_password.split("$", 1)
        check_key = hashlib.pbkdf2_hmac(
            "sha256",
            plain_password.encode("utf-8"),
            salt.encode("utf-8"),
            100000,
        )
        return hmac.compare_digest(check_key.hex(), key_hex)
    except (ValueError, TypeError):
        return False


class Base(DeclarativeBase):
    pass


class Student(Base):
    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # e.g., STU_001
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    major: Mapped[str] = mapped_column(String(100), nullable=False, default="Computer Engineering")
    year: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    gpa: Mapped[float] = mapped_column(Float, nullable=False, default=3.00)
    academic_standing_raw: Mapped[str] = mapped_column(
        "academic_standing", String(20), nullable=False, default="GOOD_STANDING"
    )
    financial_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_courses_raw: Mapped[str] = mapped_column(
        "completed_courses", Text, nullable=False, default="[]"
    )
    enrolled_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    @property
    def academic_standing(self) -> AcademicStanding:
        try:
            return AcademicStanding(self.academic_standing_raw)
        except ValueError:
            return AcademicStanding.GOOD_STANDING

    @academic_standing.setter
    def academic_standing(self, standing: AcademicStanding | str) -> None:
        if isinstance(standing, AcademicStanding):
            self.academic_standing_raw = standing.value
        else:
            self.academic_standing_raw = str(standing)

    @property
    def completed_courses(self) -> list[str]:
        """Expose completed course codes as a parsed list of strings."""
        if not self.completed_courses_raw:
            return []
        try:
            parsed = json.loads(self.completed_courses_raw)
            if isinstance(parsed, list):
                return [str(c) for c in parsed]
        except (json.JSONDecodeError, TypeError):
            return [c.strip() for c in self.completed_courses_raw.split(",") if c.strip()]
        return []

    @completed_courses.setter
    def completed_courses(self, courses: list[str] | str) -> None:
        if isinstance(courses, list):
            self.completed_courses_raw = json.dumps(courses)
        else:
            self.completed_courses_raw = str(courses)

    def set_password(self, plain_password: str) -> None:
        """Hash and update student's password."""
        self.password_hash = hash_password(plain_password)

    def check_password(self, plain_password: str) -> bool:
        """Verify plain password against stored hash."""
        return verify_password(plain_password, self.password_hash)

    def to_dto(self) -> StudentDTO:
        """Convert ORM model to shared StudentDTO schema."""
        return StudentDTO(
            id=self.id,
            name=self.name,
            gpa=self.gpa,
            academic_standing=self.academic_standing,
            financial_hold=self.financial_hold,
            completed_courses=self.completed_courses,
            enrolled_credits=self.enrolled_credits,
            max_credits=self.max_credits,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "major": self.major,
            "year": self.year,
            "gpa": self.gpa,
            "academic_standing": self.academic_standing.value,
            "financial_hold": self.financial_hold,
            "completed_courses": self.completed_courses,
            "enrolled_credits": self.enrolled_credits,
            "max_credits": self.max_credits,
        }
