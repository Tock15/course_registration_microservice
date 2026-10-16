"""Mediator coordinating validation, seat allocation and events."""
import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.events import CourseDroppedEvent, EnrollmentConfirmedEvent
from common.schemas import (
    AcademicStanding,
    EnrollmentStatus,
    PrereqValidationResponse,
    StudentDTO,
    StudentEligibilityResponse,
)
from enrollment_service import repository, waitlist
from enrollment_service.clients import CatalogClient, StudentClient
from enrollment_service.database import run_with_retry
from enrollment_service.events import EventPublisher, WaitlistJoinedEvent
from enrollment_service.models import Enrollment
from enrollment_service.schemas import (
    ClaimResult,
    EnrollmentRecord,
    EnrollmentResult,
    MyEnrollmentsResponse,
    WaitlistEntry,
)
from enrollment_service.seat_allocator import SeatAllocator
from enrollment_service.validation import (
    ActiveEnrollment,
    EnrollmentContext,
    RuleValidationEngine,
    default_rules,
)


def compute_eligibility_from_profile(profile: StudentDTO) -> StudentEligibilityResponse:
    """Evaluate student registration eligibility directly from StudentDTO profile."""
    # 1. Financial Hold Check
    if profile.financial_hold:
        return StudentEligibilityResponse(
            student_id=profile.id,
            is_eligible=False,
            reason="Financial hold active on account. Clear outstanding dues before registering.",
            remaining_credits=0,
        )

    # 2. Academic Suspension Check
    if profile.academic_standing == AcademicStanding.SUSPENDED:
        return StudentEligibilityResponse(
            student_id=profile.id,
            is_eligible=False,
            reason="Student is under academic suspension.",
            remaining_credits=0,
        )

    # 3. Academic Probation Credit Ceiling (15 credits limit)
    effective_max_credits = profile.max_credits
    if profile.academic_standing == AcademicStanding.PROBATION:
        effective_max_credits = min(profile.max_credits, 15)

    if profile.enrolled_credits >= effective_max_credits:
        return StudentEligibilityResponse(
            student_id=profile.id,
            is_eligible=False,
            reason=f"Maximum allowed credit limit of {effective_max_credits} credits reached.",
            remaining_credits=0,
        )

    remaining_credits = max(0, effective_max_credits - profile.enrolled_credits)
    return StudentEligibilityResponse(
        student_id=profile.id,
        is_eligible=True,
        reason=None,
        remaining_credits=remaining_credits,
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
        async def _read_active():
            async with self._sessions() as session:
                return await repository.active_enrollments(session, student_id)

        profile, section, active = await asyncio.gather(
            self._student.get_profile(student_id),
            self._catalog.get_section(section_id),
            run_with_retry(_read_active),
        )
        eligibility = compute_eligibility_from_profile(profile)

        if section.course_credits is not None:
            course_credits = section.course_credits
            completed_set = {c.strip().upper() for c in profile.completed_courses}
            missing = sorted(
                [
                    r
                    for r in section.direct_prereqs
                    if r.strip().upper() not in completed_set
                ]
            )
            if missing:
                prereqs = PrereqValidationResponse(
                    is_valid=False,
                    missing_prereqs=missing,
                    message=f"Missing required prerequisite(s): {', '.join(missing)}",
                )
            else:
                prereqs = PrereqValidationResponse(
                    is_valid=True,
                    missing_prereqs=[],
                    message="All prerequisites satisfied.",
                )
        else:
            course, prereqs = await asyncio.gather(
                self._catalog.get_course(section.course_code),
                self._catalog.validate_prereqs(
                    student_id, section.course_code, profile.completed_courses
                ),
            )
            course_credits = course.credits

        ctx = EnrollmentContext(
            student=profile,
            eligibility=eligibility,
            section=section,
            course_credits=course_credits,
            prereq_result=prereqs,
            active_enrollments=tuple(_snapshot(row) for row in active),
        )
        verdict = self._rules.validate(ctx)
        if not verdict.is_valid:
            async def _record_reject():
                async with self._sessions() as session, session.begin():
                    repository.record_audit(
                        session,
                        student_id=student_id,
                        section_id=section.id,
                        action="REJECTED",
                        to_status=EnrollmentStatus.REJECTED.value,
                        detail=f"{verdict.rule_name}: {verdict.error_message}",
                    )

            await run_with_retry(_record_reject)
            return EnrollmentResult(
                student_id=student_id,
                section_id=section.id,
                course_code=section.course_code,
                status=EnrollmentStatus.REJECTED,
                message=verdict.error_message or "Enrollment rejected.",
                rule_name=verdict.rule_name,
            )

        allocation = await self._seats.allocate(student_id, section, course_credits)

        if allocation.status is EnrollmentStatus.ENROLLED:
            self._publisher.publish(
                EnrollmentConfirmedEvent(
                    student_id=student_id,
                    section_id=section.id,
                    course_code=section.course_code,
                    credits=course_credits,
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
                f"{allocation.waitlist_position}; you will get a time-limited "
                "seat offer to claim when a seat frees up."
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
        async def _do_drop():
            async with self._sessions() as session, session.begin():
                return await waitlist.drop(session, student_id, section_id)

        outcome = await run_with_retry(_do_drop)

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
        if outcome.offered:
            self._publisher.publish(outcome.offered.event())

        if outcome.previous_status is EnrollmentStatus.WAITLISTED:
            message = f"Removed from the waitlist for section {section_id}."
        else:
            message = (
                f"Declined the seat offer for section {section_id}."
                if outcome.previous_status is EnrollmentStatus.OFFERED
                else f"Dropped section {section_id}."
            )
            if outcome.offered:
                message += " The seat was offered to the next waitlisted student."

        return EnrollmentResult(
            enrollment_id=outcome.enrollment_id,
            student_id=student_id,
            section_id=section_id,
            course_code=outcome.course_code,
            status=EnrollmentStatus.DROPPED,
            message=message,
        )

    async def claim(self, student_id: str, offer_id: int) -> ClaimResult:
        async def _do_claim():
            async with self._sessions() as session, session.begin():
                return await repository.claim_offer(session, offer_id, student_id)

        row = await run_with_retry(_do_claim)
        if row is None:
            return ClaimResult(
                status="EXPIRED_OR_ALREADY_CLAIMED",
                message="This offer has expired or was already claimed.",
            )

        self._publisher.publish(
            EnrollmentConfirmedEvent(
                student_id=student_id,
                section_id=row.section_id,
                course_code=row.course_code,
                credits=row.credits,
            )
        )
        return ClaimResult(
            status="CLAIMED",
            message=f"Seat claimed. You are now enrolled in {row.course_code}.",
            enrollment_id=row.id,
            section_id=row.section_id,
            course_code=row.course_code,
        )

    async def my_waitlist(self, student_id: str) -> list[WaitlistEntry]:
        async def _fetch():
            async with self._sessions() as session, session.begin():
                return [
                    WaitlistEntry(
                        offer_id=row.id,
                        section_id=row.section_id,
                        course_code=row.course_code,
                        position=pos,
                        status=EnrollmentStatus(row.status),
                        expires_at=row.offer_expires_at,
                    )
                    for row, pos in await waitlist.student_queue(session, student_id)
                ]

        return await run_with_retry(_fetch)

    async def list_for_student(self, student_id: str) -> MyEnrollmentsResponse:
        async def _fetch():
            async with self._sessions() as session, session.begin():
                rows = await repository.student_enrollments(session, student_id)
                return [
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
                        offer_expires_at=(
                            row.offer_expires_at
                            if row.status == EnrollmentStatus.OFFERED.value
                            else None
                        ),
                        reason=row.reason,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                    )
                    for row in rows
                ]

        records = await run_with_retry(_fetch)

        total = sum(r.credits for r in records if r.status is EnrollmentStatus.ENROLLED)
        return MyEnrollmentsResponse(
            student_id=student_id, total_credits=total, enrollments=records
        )
