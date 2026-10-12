"""
enrollment_service/tests/test_validation.py
Unit tests for the Strategy Pattern rule engine (pure, no I/O).
"""
from common.schemas import (
    PrereqValidationResponse,
    SectionDTO,
    StudentDTO,
    StudentEligibilityResponse,
    ValidationResult,
)
from enrollment_service.validation import (
    ActiveEnrollment,
    CreditCapRule,
    DuplicateEnrollmentRule,
    EnrollmentContext,
    PrerequisiteRule,
    RuleValidationEngine,
    StudentEligibilityRule,
    TimeConflictRule,
    ValidationStrategy,
    default_rules,
)

CS301_SEC_101 = SectionDTO(
    id=101,
    course_code="CS301",
    section_number=1,
    capacity=10,
    schedule_days=["MON", "WED"],
    start_time="09:00",
    end_time="10:30",
)


def active(
    section_id: int = 103,
    course_code: str = "CS101",
    credits: int = 3,
    days: tuple[str, ...] = ("TUE", "THU"),
    start: str = "09:00",
    end: str = "10:30",
    status: str = "ENROLLED",
) -> ActiveEnrollment:
    return ActiveEnrollment(
        section_id=section_id,
        course_code=course_code,
        credits=credits,
        status=status,
        schedule_days=days,
        start_time=start,
        end_time=end,
    )


def make_ctx(
    *,
    section: SectionDTO = CS301_SEC_101,
    credits: int = 3,
    max_credits: int = 22,
    eligible: bool = True,
    reason: str | None = None,
    missing: tuple[str, ...] = (),
    actives: tuple[ActiveEnrollment, ...] = (),
) -> EnrollmentContext:
    return EnrollmentContext(
        student=StudentDTO(id="STU_001", name="Test", gpa=3.2, max_credits=max_credits),
        eligibility=StudentEligibilityResponse(
            student_id="STU_001", is_eligible=eligible, reason=reason, remaining_credits=22
        ),
        section=section,
        course_credits=credits,
        prereq_result=PrereqValidationResponse(
            is_valid=not missing, missing_prereqs=list(missing)
        ),
        active_enrollments=actives,
    )


def test_all_default_rules_pass_for_clean_request():
    assert RuleValidationEngine(default_rules()).validate(make_ctx()).is_valid


def test_duplicate_rule_blocks_same_section_and_same_course():
    same_section = make_ctx(actives=(active(section_id=101, course_code="CS301"),))
    other_section = make_ctx(actives=(active(section_id=102, course_code="CS301"),))
    rule = DuplicateEnrollmentRule()
    assert not rule.validate(same_section).is_valid
    result = rule.validate(other_section)
    assert not result.is_valid
    assert "section 102" in result.error_message


def test_eligibility_rule_uses_student_service_reason():
    result = StudentEligibilityRule().validate(
        make_ctx(eligible=False, reason="Financial hold on account.")
    )
    assert not result.is_valid
    assert result.error_message == "Financial hold on account."
    assert result.rule_name == "StudentEligibilityRule"


def test_prerequisite_rule_reports_missing_courses():
    result = PrerequisiteRule().validate(make_ctx(missing=("CS102", "CS202")))
    assert not result.is_valid
    assert "CS102, CS202" in result.error_message


def test_credit_cap_counts_waitlisted_rows():
    actives = (
        active(credits=9),
        active(section_id=105, course_code="CS202", credits=9, status="WAITLISTED",
               days=("FRI",)),
    )
    assert CreditCapRule().validate(make_ctx(actives=actives, credits=4)).is_valid
    result = CreditCapRule().validate(make_ctx(actives=actives, credits=5))
    assert not result.is_valid
    assert "18 registered + 5 requested > 22" in result.error_message


def test_time_conflict_detects_overlap_only_on_shared_days():
    overlapping = active(section_id=106, course_code="CS303", days=("WED",), start="10:00",
                         end="12:00")
    back_to_back = active(section_id=104, course_code="CS102", days=("MON",), start="10:30",
                          end="12:00")
    other_day = active(section_id=103, days=("TUE",), start="09:00", end="10:30")

    rule = TimeConflictRule()
    result = rule.validate(make_ctx(actives=(overlapping,)))
    assert not result.is_valid
    assert "CS303" in result.error_message and "WED" in result.error_message
    assert rule.validate(make_ctx(actives=(back_to_back, other_day))).is_valid


def test_engine_stops_at_first_failing_rule():
    ctx = make_ctx(eligible=False, reason="Hold", missing=("CS102",), max_credits=0)
    result = RuleValidationEngine(default_rules()).validate(ctx)
    assert result.rule_name == "StudentEligibilityRule"


def test_open_closed_new_rule_plugs_in_without_engine_changes():
    class NoFridayRule(ValidationStrategy):
        name = "NoFridayRule"

        def validate(self, ctx: EnrollmentContext) -> ValidationResult:
            if "FRI" in ctx.section.schedule_days:
                return self.fail("No Friday classes allowed.")
            return self.ok()

    friday = CS301_SEC_101.model_copy(update={"schedule_days": ["FRI"]})
    engine = RuleValidationEngine([*default_rules(), NoFridayRule()])
    assert engine.validate(make_ctx()).is_valid
    result = engine.validate(make_ctx(section=friday))
    assert result.rule_name == "NoFridayRule"
