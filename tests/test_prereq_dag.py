"""
tests/test_prereq_dag.py
Unit tests for the Prerequisite Directed Acyclic Graph (DAG) Engine.
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from catalog_service.models import Base, Course, Prerequisite
from catalog_service.prereq_dag import PrereqDAG, build_dag_from_db


def test_dag_basic_prerequisites():
    dag = PrereqDAG()
    dag.add_course("CS101")
    dag.add_course("CS102")
    dag.add_prerequisite("CS102", "CS101")

    assert dag.get_direct_prereqs("CS102") == ["CS101"]
    assert dag.get_direct_prereqs("CS101") == []


def test_dag_transitive_closure():
    dag = PrereqDAG()
    # Chain: CS301 -> CS201 -> CS102 -> CS101
    dag.add_prerequisite("CS102", "CS101")
    dag.add_prerequisite("CS201", "CS102")
    dag.add_prerequisite("CS301", "CS201")

    # Direct prerequisites
    assert dag.get_direct_prereqs("CS301") == ["CS201"]

    # Transitive closure
    assert dag.get_all_prereqs("CS301") == {"CS201", "CS102", "CS101"}
    assert dag.get_all_prereqs("CS201") == {"CS102", "CS101"}
    assert dag.get_all_prereqs("CS101") == set()


def test_dag_diamond_dependency():
    dag = PrereqDAG()
    # CS401 -> CS201, CS202
    # CS201 -> CS101
    # CS202 -> CS101
    dag.add_prerequisite("CS201", "CS101")
    dag.add_prerequisite("CS202", "CS101")
    dag.add_prerequisite("CS401", "CS201")
    dag.add_prerequisite("CS401", "CS202")

    assert set(dag.get_direct_prereqs("CS401")) == {"CS201", "CS202"}
    assert dag.get_all_prereqs("CS401") == {"CS201", "CS202", "CS101"}


def test_dag_detects_self_cycle():
    dag = PrereqDAG()
    with pytest.raises(ValueError, match="Self-prerequisite cycle"):
        dag.add_prerequisite("CS101", "CS101")


def test_dag_detects_indirect_cycle():
    dag = PrereqDAG()
    dag.add_prerequisite("CS102", "CS101")
    dag.add_prerequisite("CS201", "CS102")

    # CS201 requires CS102 requires CS101.
    # Trying to add CS101 requires CS201 must fail
    assert dag.would_create_cycle("CS101", "CS201") is True

    with pytest.raises(ValueError, match="Cyclic prerequisite dependency"):
        dag.add_prerequisite("CS101", "CS201")


def test_dag_three_color_cycle_detection():
    dag = PrereqDAG()
    dag.add_course("A")
    dag.add_course("B")
    dag.add_course("C")
    # Manually inject cycle into adj list to verify 3-color DFS detector
    dag.adj["A"] = {"B"}
    dag.adj["B"] = {"C"}
    dag.adj["C"] = {"A"}

    cycles = dag.detect_cycles()
    assert len(cycles) > 0
    assert "A" in cycles[0] and "B" in cycles[0] and "C" in cycles[0]


def test_dag_topological_sort():
    dag = PrereqDAG()
    dag.add_prerequisite("CS102", "CS101")
    dag.add_prerequisite("CS201", "CS102")

    order = dag.topological_sort()
    # CS101 must come before CS102, which must come before CS201
    assert order.index("CS101") < order.index("CS102")
    assert order.index("CS102") < order.index("CS201")


def test_dag_validate_student_passed():
    dag = PrereqDAG()
    dag.add_prerequisite("CS301", "CS102")
    dag.add_prerequisite("CS301", "CS202")

    res = dag.validate_student(
        course_code="CS301",
        completed_courses=["CS101", "CS102", "CS202"],
        transitive=False,
    )
    assert res.is_valid is True
    assert res.missing_prereqs == []
    assert "All prerequisites satisfied" in (res.message or "")


def test_dag_validate_student_missing_prereqs():
    dag = PrereqDAG()
    dag.add_prerequisite("CS301", "CS102")
    dag.add_prerequisite("CS301", "CS202")

    res = dag.validate_student(
        course_code="CS301",
        completed_courses=["CS101", "CS102"],  # Missing CS202
        transitive=False,
    )
    assert res.is_valid is False
    assert res.missing_prereqs == ["CS202"]
    assert "CS202" in (res.message or "")


def test_dag_validate_student_transitive_mode():
    dag = PrereqDAG()
    dag.add_prerequisite("CS102", "CS101")
    dag.add_prerequisite("CS201", "CS102")

    # In transitive mode, student needs CS101 and CS102
    res_fail = dag.validate_student(
        course_code="CS201",
        completed_courses=["CS102"],  # lacks CS101
        transitive=True,
    )
    assert res_fail.is_valid is False
    assert res_fail.missing_prereqs == ["CS101"]

    res_pass = dag.validate_student(
        course_code="CS201",
        completed_courses=["CS101", "CS102"],
        transitive=True,
    )
    assert res_pass.is_valid is True


@pytest.mark.asyncio
async def test_build_dag_from_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        c1 = Course(code="CS101", title="Intro", credits=3)
        c2 = Course(code="CS102", title="Data Struct", credits=3)
        p = Prerequisite(course_code="CS102", required_course_code="CS101")
        session.add_all([c1, c2, p])
        await session.commit()

        # Build DAG from DB
        dag = await build_dag_from_db(session)

        assert "CS101" in dag.adj
        assert "CS102" in dag.adj
        assert dag.get_direct_prereqs("CS102") == ["CS101"]

    await engine.dispose()
