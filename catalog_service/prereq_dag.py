"""
catalog_service/prereq_dag.py
Prerequisite Directed Acyclic Graph (DAG) Engine.

Responsibilities:
  - Adjacency list representation of curriculum prerequisite dependencies
  - Cycle detection using three-color DFS and pre-insertion reachability tests
  - Transitive closure dependency traversal
  - Student prerequisite eligibility validation
  - Async database loader from catalog.db
"""
from collections import deque
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service.models import Course, Prerequisite
from common.schemas import PrereqValidationResponse


class PrereqDAG:
    """Directed Acyclic Graph (DAG) for curriculum prerequisite dependencies.

    Graph semantics:
      Node: course_code (e.g. 'CS102')
      Directed Edge (u -> v): course u requires preceding course v.
    """

    def __init__(self) -> None:
        # Maps course_code -> set of required preceding courses
        self.adj: dict[str, set[str]] = {}

    def add_course(self, course_code: str) -> None:
        """Register a course vertex in the graph."""
        code = course_code.strip().upper()
        if code not in self.adj:
            self.adj[code] = set()

    def add_prerequisite(self, course_code: str, required_course_code: str) -> None:
        """Add a prerequisite dependency edge (course_code -> required_course_code).

        Raises ValueError if adding this edge introduces a cyclic dependency.
        """
        src = course_code.strip().upper()
        target = required_course_code.strip().upper()

        if src == target:
            raise ValueError(
                f"Self-prerequisite cycle detected: course '{src}' cannot require itself."
            )

        if self.would_create_cycle(src, target):
            raise ValueError(
                f"Cyclic prerequisite dependency detected: adding '{src}' -> '{target}' creates a cycle."
            )

        self.add_course(src)
        self.add_course(target)
        self.adj[src].add(target)

    def would_create_cycle(self, course_code: str, required_course_code: str) -> bool:
        """Check if adding edge (course_code -> required_course_code) would form a cycle.

        Adding src -> target creates a cycle if target can ALREADY reach src
        through existing prerequisite dependencies.
        """
        src = course_code.strip().upper()
        target = required_course_code.strip().upper()

        if src == target:
            return True

        if target not in self.adj or src not in self.adj:
            return False

        # BFS reachability search from target to src
        visited: set[str] = set()
        queue: deque[str] = deque([target])

        while queue:
            curr = queue.popleft()
            if curr == src:
                return True
            if curr not in visited:
                visited.add(curr)
                for neighbor in self.adj.get(curr, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)

        return False

    def detect_cycles(self) -> list[list[str]]:
        """Three-color Depth First Search (DFS) cycle detector.

        Returns a list of cycle path traces, or an empty list if graph is acyclic.
        Colors:
          0: White (unvisited)
          1: Gray (currently visiting on recursion stack)
          2: Black (visited and fully processed)
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        colors: dict[str, int] = {node: WHITE for node in self.adj}
        path: list[str] = []
        cycles: list[list[str]] = []

        def dfs(node: str) -> None:
            colors[node] = GRAY
            path.append(node)

            for neighbor in self.adj.get(node, set()):
                if colors.get(neighbor) == GRAY:
                    # Found a cycle back-edge!
                    cycle_start = path.index(neighbor)
                    cycles.append(path[cycle_start:] + [neighbor])
                elif colors.get(neighbor, WHITE) == WHITE:
                    dfs(neighbor)

            path.pop()
            colors[node] = BLACK

        for node in list(self.adj.keys()):
            if colors.get(node) == WHITE:
                dfs(node)

        return cycles

    def get_direct_prereqs(self, course_code: str) -> list[str]:
        """Return immediate prerequisite course codes for the given course."""
        code = course_code.strip().upper()
        return sorted(self.adj.get(code, set()))

    def get_all_prereqs(self, course_code: str) -> set[str]:
        """Return the transitive closure of all prerequisite dependencies.

        For example, if CS301 -> CS201 and CS201 -> CS101, returns {'CS201', 'CS101'}.
        """
        code = course_code.strip().upper()
        all_prereqs: set[str] = set()
        queue: deque[str] = deque(self.adj.get(code, set()))

        while queue:
            curr = queue.popleft()
            if curr not in all_prereqs:
                all_prereqs.add(curr)
                for neighbor in self.adj.get(curr, set()):
                    if neighbor not in all_prereqs:
                        queue.append(neighbor)

        return all_prereqs

    def topological_sort(self) -> list[str]:
        """Return courses in topological dependency order (Kahn's Algorithm).

        Courses with zero prerequisites appear first.
        Raises ValueError if graph has a cycle.
        """
        # Calculate in-degree: number of prerequisites a course has
        in_degree: dict[str, int] = {node: len(self.adj[node]) for node in self.adj}

        # Queue nodes with 0 prerequisites
        queue: deque[str] = deque([node for node, deg in in_degree.items() if deg == 0])
        order: list[str] = []

        # We also need reverse adjacency to propagate removals
        rev_adj: dict[str, set[str]] = {node: set() for node in self.adj}
        for src, targets in self.adj.items():
            for target in targets:
                rev_adj.setdefault(target, set()).add(src)

        while queue:
            curr = queue.popleft()
            order.append(curr)

            # For courses that required curr, reduce their prerequisite count
            for dependent in rev_adj.get(curr, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(self.adj):
            raise ValueError("Graph contains a cycle; topological sort not possible.")

        return order

    def validate_student(
        self,
        course_code: str,
        completed_courses: Iterable[str],
        transitive: bool = False,
    ) -> PrereqValidationResponse:
        """Validate whether a student satisfies the prerequisite requirements for a course.

        Args:
            course_code: The course the student wants to register for.
            completed_courses: Collection of course codes the student has passed.
            transitive: If True, evaluates the entire dependency tree. Default False
                        (evaluates direct requirements).

        Returns:
            PrereqValidationResponse with is_valid, missing_prereqs, and message.
        """
        code = course_code.strip().upper()
        completed_set = {c.strip().upper() for c in completed_courses}

        if transitive:
            required = self.get_all_prereqs(code)
        else:
            required = set(self.get_direct_prereqs(code))

        missing = sorted([req for req in required if req not in completed_set])

        if missing:
            return PrereqValidationResponse(
                is_valid=False,
                missing_prereqs=missing,
                message=f"Missing required prerequisite(s): {', '.join(missing)}",
            )

        return PrereqValidationResponse(
            is_valid=True,
            missing_prereqs=[],
            message="All prerequisites satisfied.",
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize adjacency graph to dictionary."""
        return {node: sorted(neighbors) for node, neighbors in self.adj.items()}


async def build_dag_from_db(session: AsyncSession) -> PrereqDAG:
    """Async factory reading Course and Prerequisite records from catalog.db."""
    dag = PrereqDAG()

    # 1. Load all course nodes
    courses_query = await session.execute(select(Course.code))
    for (code,) in courses_query.all():
        dag.add_course(code)

    # 2. Load all prerequisite edges
    prereqs_query = await session.execute(
        select(Prerequisite.course_code, Prerequisite.required_course_code)
    )
    for c_code, req_code in prereqs_query.all():
        # Edge insertion (handles unindexed courses gracefully if needed)
        dag.add_course(c_code)
        dag.add_course(req_code)
        dag.adj[c_code.strip().upper()].add(req_code.strip().upper())

    return dag
