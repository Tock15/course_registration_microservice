"""
enrollment_service/orchestrator.py
Mediator Pattern: coordinates Student, Catalog, the rule engine, seat
allocation and event publishing so none of them know about each other.

Enroll:
  Phase 1  parallel read-only checks (profile, eligibility, section), then
           parallel course credits + prerequisite DAG validation.
  Phase 2  pure rule evaluation (Strategy) over the gathered context.
  Phase 3  seat allocation against Catalog (seat_allocator.py).
  Phase 4  fire-and-forget domain events after commit.
"""
import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.events import CourseDroppedEvent, EnrollmentConfirmedEvent
from common.schemas import EnrollmentStatus
from enrollment_service import repository, waitlist
from enrollment_service.clients import CatalogClient, StudentClient
from enrollment_service.events import EventPublisher, WaitlistJoinedEvent
from enrollment_service.models import Enrollment
from enrollment_service.schemas import (
    EnrollmentRecord,
    EnrollmentResult,
    MyEnrollmentsResponse,
)
from enrollment_service.seat_allocator import SeatAllocator
from enrollment_service.validation import (
    ActiveEnrollment,
    EnrollmentContext,
    RuleValidationEngine,
    default_rules,
)


def _snapshot(row: Enrollment) -> ActiveEnrollment:
    return ActiveEnrollment(
        section_id=row.section_id,
        course_code=row.course_code,
        credits=row.credits,
        status=row.status,
        schedule_days=tuple(row.schedule_days),
        start_time=row.start_time,
        end_time=row.end_time,
    )


class EnrollmentOrchestrator:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        student: StudentClient,
        catalog: CatalogClient,
        publisher: EventPublisher,
        rules: RuleValidationEngine | None = None,
    ) -> None:
        self._sessions = sessions
        self._student = student
        self._catalog = catalog
        self._publisher = publisher
        self._rules = rules or RuleValidationEngine(default_rules())
        self._seats = SeatAllocator(sessions, catalog)

    async def enroll(self, student_id: str, section_id: int) -> EnrollmentResult:
        profile, eligibility, section = await asyncio.gather(
            self._student.get_profile(student_id),
            self._student.get_eligibility(student_id),
            self._catalog.get_section(section_id),
        )
        course, prereqs = await asyncio.gather(
            self._catalog.get_course(section.course_code),
            self._catalog.validate_prereqs(
                student_id, section.course_code, profile.completed_courses
            ),
        )
        async with self._sessions() as session, session.begin():
            active = await repository.active_enrollments(session, student_id)

        ctx = EnrollmentContext(
            student=profile,
            eligibility=eligibility,
            section=section,
            course_credits=course.credits,
            prereq_result=prereqs,
            active_enrollments=tuple(_snapshot(row) for row in active),
        )
        verdict = self._rules.validate(ctx)
        if not verdict.is_valid:
            async with self._sessions() as session, session.begin():
                repository.record_audit(
                    session,
                    student_id=student_id,
                    section_id=section.id,
                    action="REJECTED",
                    to_status=EnrollmentStatus.REJECTED.value,
                    detail=f"{verdict.rule_name}: {verdict.error_message}",
                )
            return EnrollmentResult(
                student_id=student_id,
                section_id=section.id,
                course_code=section.course_code,
                status=EnrollmentStatus.REJECTED,
                message=verdict.error_message or "Enrollment rejected.",
                rule_name=verdict.rule_name,
            )

        allocation = await self._seats.allocate(student_id, section, course.credits)

        if allocation.status is EnrollmentStatus.ENROLLED:
            self._publisher.publish(
                EnrollmentConfirmedEvent(
                    student_id=student_id,
                    section_id=section.id,
                    course_code=section.course_code,
                    credits=course.credits,
                )
            )
            message = (
                f"Successfully enrolled in {section.course_code} "
                f"section {section.section_number}."
            )
        else:
            self._publisher.publish(
                WaitlistJoinedEvent(
                    student_id=student_id,
                    section_id=section.id,
                    course_code=section.course_code,
                    position=allocation.waitlist_position or 0,
                )
            )
            message = (
                "Section full. Placed on waitlist at position "
                f"{allocation.waitlist_position}; you will be enrolled "
                "automatically when a seat frees up."
            )

        return EnrollmentResult(
            enrollment_id=allocation.enrollment_id,
            student_id=student_id,
            section_id=section.id,
            course_code=section.course_code,
            status=allocation.status,
            message=message,
            waitlist_position=allocation.waitlist_position,
        )

    async def drop(self, student_id: str, section_id: int) -> EnrollmentResult:
        async with self._sessions() as session, session.begin():
            outcome = await waitlist.drop(session, student_id, section_id)

        if outcome.pending_release_key:
            await self._seats.release(
                outcome.enrollment_id, section_id, outcome.pending_release_key
            )

        self._publisher.publish(
            CourseDroppedEvent(
                student_id=student_id,
                section_id=section_id,
                course_code=outcome.course_code,
            )
        )
        if outcome.promoted:
            self._publisher.publish(
                EnrollmentConfirmedEvent(
                    student_id=outcome.promoted.student_id,
                    section_id=section_id,
                    course_code=outcome.promoted.course_code,
                    credits=outcome.promoted.credits,
                )
            )

        if outcome.previous_status is EnrollmentStatus.WAITLISTED:
            message = f"Removed from the waitlist for section {section_id}."
        elif outcome.promoted:
            message = (
                f"Dropped section {section_id}. "
                "Your seat was passed to the next waitlisted student."
            )
        else:
            message = f"Dropped section {section_id}."

        return EnrollmentResult(
            enrollment_id=outcome.enrollment_id,
            student_id=student_id,
            section_id=section_id,
            course_code=outcome.course_code,
            status=EnrollmentStatus.DROPPED,
            message=message,
        )

    async def list_for_student(self, student_id: str) -> MyEnrollmentsResponse:
        async with self._sessions() as session, session.begin():
            rows = await repository.student_enrollments(session, student_id)
            records = [
                EnrollmentRecord(
                    enrollment_id=row.id,
                    section_id=row.section_id,
                    course_code=row.course_code,
                    credits=row.credits,
                    status=EnrollmentStatus(row.status),
                    schedule_days=row.schedule_days,
                    start_time=row.start_time,
                    end_time=row.end_time,
                    waitlist_position=(
                        await waitlist.position(session, row.id)
                        if row.status == EnrollmentStatus.WAITLISTED.value
                        else None
                    ),
                    reason=row.reason,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]

        total = sum(r.credits for r in records if r.status is EnrollmentStatus.ENROLLED)
        return MyEnrollmentsResponse(
            student_id=student_id, total_credits=total, enrollments=records
        )
