"""HTTP clients for the Student and Catalog services."""
from urllib.parse import quote

import httpx

from common.schemas import (
    CourseDTO,
    PrereqValidationRequest,
    PrereqValidationResponse,
    SectionDTO,
    StudentDTO,
    StudentEligibilityResponse,
)


class UpstreamUnavailable(Exception):
    def __init__(self, service: str, detail: str) -> None:
        self.service = service
        self.detail = detail
        super().__init__(f"{service} unavailable: {detail}")


class UpstreamNotFound(Exception):
    def __init__(self, service: str, detail: str) -> None:
        self.service = service
        self.detail = detail
        super().__init__(detail)


class _ServiceClient:
    service_name = "upstream"

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def _request(
        self, method: str, path: str, json: dict | None = None
    ) -> httpx.Response:
        try:
            response = await self._http.request(
                method, f"{self._base_url}{path}", json=json
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(
                self.service_name, f"{type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 500:
            raise UpstreamUnavailable(
                self.service_name, f"HTTP {response.status_code} on {method} {path}"
            )
        return response

    def _unexpected(self, response: httpx.Response) -> UpstreamUnavailable:
        return UpstreamUnavailable(
            self.service_name,
            f"Unexpected HTTP {response.status_code} on "
            f"{response.request.method} {response.request.url.path}",
        )


class StudentClient(_ServiceClient):
    service_name = "student_service"

    async def get_profile(self, student_id: str) -> StudentDTO:
        res = await self._request("GET", f"/students/{quote(student_id)}")
        if res.status_code == 404:
            raise UpstreamNotFound(self.service_name, f"Student '{student_id}' not found.")
        if res.status_code != 200:
            raise self._unexpected(res)
        return StudentDTO.model_validate(res.json())

    async def get_eligibility(self, student_id: str) -> StudentEligibilityResponse:
        res = await self._request("GET", f"/students/{quote(student_id)}/eligibility")
        if res.status_code == 404:
            raise UpstreamNotFound(self.service_name, f"Student '{student_id}' not found.")
        if res.status_code != 200:
            raise self._unexpected(res)
        return StudentEligibilityResponse.model_validate(res.json())


class CatalogClient(_ServiceClient):
    """Catalog reads plus the idempotent seat reserve/release contract."""

    service_name = "catalog_service"

    async def get_section(self, section_id: int) -> SectionDTO:
        res = await self._request("GET", f"/sections/{section_id}")
        if res.status_code == 404:
            raise UpstreamNotFound(self.service_name, f"Section {section_id} not found.")
        if res.status_code != 200:
            raise self._unexpected(res)
        return SectionDTO.model_validate(res.json())

    async def get_course(self, course_code: str) -> CourseDTO:
        res = await self._request("GET", f"/courses/{quote(course_code)}")
        if res.status_code == 404:
            raise UpstreamNotFound(self.service_name, f"Course '{course_code}' not found.")
        if res.status_code != 200:
            raise self._unexpected(res)
        return CourseDTO.model_validate(res.json())

    async def validate_prereqs(
        self, student_id: str, course_code: str, completed_courses: list[str]
    ) -> PrereqValidationResponse:
        payload = PrereqValidationRequest(
            student_id=student_id,
            course_code=course_code,
            completed_courses=completed_courses,
        )
        res = await self._request("POST", "/validate-prereqs", json=payload.model_dump())
        if res.status_code != 200:
            raise self._unexpected(res)
        return PrereqValidationResponse.model_validate(res.json())

    async def reserve_seat(self, section_id: int, seat_key: str, attempts: int = 2) -> bool:
        """Return True if a seat is held for seat_key, False if full."""
        for attempt in range(1, attempts + 1):
            try:
                res = await self._request(
                    "POST",
                    f"/sections/{section_id}/seats/reserve",
                    json={"seat_key": seat_key},
                )
            except UpstreamUnavailable:
                if attempt == attempts:
                    raise
                continue
            if res.status_code == 200:
                return bool(res.json().get("reserved", True))
            if res.status_code == 409:
                return False
            if res.status_code == 404:
                raise UpstreamNotFound(
                    self.service_name, f"Section {section_id} not found."
                )
            raise self._unexpected(res)
        raise AssertionError("unreachable")

    async def release_seat(self, section_id: int, seat_key: str) -> None:
        """Return the seat held by seat_key (no-op if unknown or already released)."""
        res = await self._request(
            "POST",
            f"/sections/{section_id}/seats/release",
            json={"seat_key": seat_key},
        )
        if res.status_code not in (200, 404):
            raise self._unexpected(res)
