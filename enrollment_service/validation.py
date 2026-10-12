"""
enrollment_service/validation.py
Strategy Pattern: pluggable enrollment rules behind a common interface.

The orchestrator (Mediator) gathers every remote fact up front, so each rule
is a pure function of an EnrollmentContext: no I/O, trivially unit-testable.
Adding a rule means adding a class to the engine's list (Open/Closed).

WAITLISTED rows count as active for the credit and conflict rules, so a later
auto-promotion off the waitlist can never break either limit.
"""
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from common.schemas import (
    PrereqValidationResponse,
    SectionDTO,
    StudentDTO,
    StudentEligibilityResponse,
    ValidationResult,
)


@dataclass(frozen=True)
class ActiveEnrollment:
    """Snapshot of one of the student's active (STAGED/ENROLLED/WAITLISTED) rows."""

    section_id: int
    course_code: str
    credits: int
    status: str
    schedule_days: tuple[str, ...]
    start_time: str
    end_time: str


@dataclass(frozen=True)
class EnrollmentContext:
    """Everything the rules need, assembled by the orchestrator."""

    student: StudentDTO
    eligibility: StudentEligibilityResponse
    section: SectionDTO
    course_credits: int
    prereq_result: PrereqValidationResponse
    active_enrollments: tuple[ActiveEnrollment, ...] = field(default_factory=tuple)


class ValidationStrategy(ABC):
    name: ClassVar[str]

    @abstractmethod
    def validate(self, ctx: EnrollmentContext) -> ValidationResult:
        """Encapsulates one distinct validation algorithm."""

    def ok(self) -> ValidationResult:
        return ValidationResult(is_valid=True, rule_name=self.name)

    def fail(self, message: str) -> ValidationResult:
        return ValidationResult(is_valid=False, error_message=message, rule_name=self.name)


class DuplicateEnrollmentRule(ValidationStrategy):
    name = "DuplicateEnrollmentRule"

    def validate(self, ctx: EnrollmentContext) -> ValidationResult:
        for active in ctx.active_enrollments:
            if active.section_id == ctx.section.id:
                return self.fail(
                    f"Already {active.status} in section {ctx.section.id}."
                )
            if active.course_code == ctx.section.course_code:
                return self.fail(
                    f"Already {active.status} in section {active.section_id} "
                    f"of course {active.course_code}."
                )
        return self.ok()


class StudentEligibilityRule(ValidationStrategy):
    name = "StudentEligibilityRule"

    def validate(self, ctx: EnrollmentContext) -> ValidationResult:
        if not ctx.eligibility.is_eligible:
            return self.fail(
                ctx.eligibility.reason or "Student is not eligible to register."
            )
        return self.ok()


class PrerequisiteRule(ValidationStrategy):
    name = "PrerequisiteRule"

    def validate(self, ctx: EnrollmentContext) -> ValidationResult:
        result = ctx.prereq_result
        if not result.is_valid:
            missing = ", ".join(result.missing_prereqs)
            return self.fail(
                result.message or f"Missing required prerequisite(s): {missing}"
            )
        return self.ok()


class CreditCapRule(ValidationStrategy):
    name = "CreditCapRule"

    def validate(self, ctx: EnrollmentContext) -> ValidationResult:
        current = sum(a.credits for a in ctx.active_enrollments)
        limit = ctx.student.max_credits
        if current + ctx.course_credits > limit:
            return self.fail(
                f"Credit limit exceeded: {current} registered + "
                f"{ctx.course_credits} requested > {limit} maximum."
            )
        return self.ok()


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


class TimeConflictRule(ValidationStrategy):
    name = "TimeConflictRule"

    def validate(self, ctx: EnrollmentContext) -> ValidationResult:
        days = {d.upper() for d in ctx.section.schedule_days}
        start, end = _minutes(ctx.section.start_time), _minutes(ctx.section.end_time)

        for active in ctx.active_enrollments:
            shared = days & {d.upper() for d in active.schedule_days}
            if not shared:
                continue
            if start < _minutes(active.end_time) and _minutes(active.start_time) < end:
                day = min(shared)
                return self.fail(
                    f"Schedule conflict with {active.course_code} "
                    f"(section {active.section_id}) on {day} "
                    f"{active.start_time}-{active.end_time}."
                )
        return self.ok()


class RuleValidationEngine:
    def __init__(self, strategies: Sequence[ValidationStrategy]) -> None:
        self.strategies = list(strategies)

    def validate(self, ctx: EnrollmentContext) -> ValidationResult:
        """Run rules in order and stop at the first failure."""
        for strategy in self.strategies:
            result = strategy.validate(ctx)
            if not result.is_valid:
                return result
        return ValidationResult(is_valid=True)


def default_rules() -> list[ValidationStrategy]:
    """Cheapest and most specific rules first."""
    return [
        DuplicateEnrollmentRule(),
        StudentEligibilityRule(),
        PrerequisiteRule(),
        CreditCapRule(),
        TimeConflictRule(),
    ]
