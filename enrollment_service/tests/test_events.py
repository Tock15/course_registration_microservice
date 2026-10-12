"""
enrollment_service/tests/test_events.py
The HTTP event publisher delivers events and never fails the caller when the
Notification Service is down (NFR: fault tolerance).
"""
import json

import httpx

from common.events import EnrollmentConfirmedEvent
from enrollment_service.events import HttpEventPublisher, WaitlistJoinedEvent


async def test_publisher_posts_event_json_to_notification_service():
    received: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/events"
        received.append(json.loads(request.content))
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        publisher = HttpEventPublisher(http, "http://notification.test")
        publisher.publish(
            WaitlistJoinedEvent(
                student_id="STU_001", section_id=105, course_code="CS202", position=3
            )
        )
        await publisher.aclose()

    assert received[0]["event_type"] == "WAITLIST_JOINED"
    assert received[0]["position"] == 3


async def test_publisher_swallows_notification_outage():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("notification down", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        publisher = HttpEventPublisher(http, "http://notification.test")
        publisher.publish(
            EnrollmentConfirmedEvent(student_id="STU_001", section_id=101, course_code="CS301")
        )
        await publisher.aclose()
