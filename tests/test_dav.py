"""The aiohttp CalDAV client against iCloud-like answers."""

from datetime import datetime, timezone

import pytest

from homeassistant.core import HomeAssistant

from custom_components.calendar_sync.caldav_provider import CalDavProvider
from custom_components.calendar_sync.models import ProviderAuthError, ProviderError

BASE = "https://caldav.icloud.com/"
PART = "https://p53-caldav.icloud.com"


def _ms(*responses: str) -> str:
    return '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + "".join(responses) + "</d:multistatus>"


PRINCIPAL = _ms(
    "<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal>"
    "<d:href>/123/principal/</d:href></d:current-user-principal></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
)
HOME = _ms(
    "<d:response><d:href>/123/principal/</d:href><d:propstat><d:prop><c:calendar-home-set>"
    f"<d:href>{PART}:443/123/calendars/</d:href></c:calendar-home-set></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
)


def _cal(path: str, name: str, comps: str, privs: str) -> str:
    return (
        f"<d:response><d:href>{path}</d:href><d:propstat><d:prop>"
        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
        f"<d:displayname>{name}</d:displayname>"
        f"<c:supported-calendar-component-set>{comps}</c:supported-calendar-component-set>"
        f"<d:current-user-privilege-set>{privs}</d:current-user-privilege-set>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "<d:propstat><d:prop><d:getctag/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        "</d:response>"
    )


WRITE = "<d:privilege><d:read/></d:privilege><d:privilege><d:write/></d:privilege>"
READ = "<d:privilege><d:read/></d:privilege>"
EVENT = '<c:comp name="VEVENT"/>'
TODO = '<c:comp name="VTODO"/>'

CALENDARS = _ms(
    "<d:response><d:href>/123/calendars/</d:href><d:propstat><d:prop>"
    "<d:resourcetype><d:collection/></d:resourcetype></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
    _cal("/123/calendars/home/", "Marcel", EVENT, WRITE),
    _cal("/123/calendars/birthdays/", "Geburtstag", EVENT, READ),
    _cal("/123/calendars/tasks/", "Erinnerungen", TODO, WRITE),
    "<d:response><d:href>/123/calendars/inbox/</d:href><d:propstat><d:prop>"
    "<d:resourcetype><d:collection/><c:schedule-inbox/></d:resourcetype></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
)

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:e1\r\n"
    "DTSTART:20261118T090000Z\r\nDTEND:20261118T100000Z\r\nSUMMARY:Arzt &amp; Co\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)
REPORT = _ms(
    "<d:response><d:href>/123/calendars/home/ABC-1.ics</d:href><d:propstat><d:prop>"
    f'<d:getetag>"1"</d:getetag><c:calendar-data>{ICS}</c:calendar-data>'
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
)


@pytest.fixture
def icloud(aioclient_mock):
    aioclient_mock.request("PROPFIND", BASE, text=PRINCIPAL, status=207)
    aioclient_mock.request("PROPFIND", f"{BASE}123/principal/", text=HOME, status=207)
    aioclient_mock.request("PROPFIND", f"{PART}:443/123/calendars/", text=CALENDARS, status=207)
    aioclient_mock.request("REPORT", f"{PART}:443/123/calendars/home/", text=REPORT, status=207)
    return aioclient_mock


async def test_discovery_in_three_requests(hass: HomeAssistant, icloud) -> None:
    provider = CalDavProvider(hass, BASE, "me@icloud.com", "app-pass")
    calendars = await provider.async_list_calendars()
    assert [(c.name, c.writable) for c in calendars] == [("Marcel", True), ("Geburtstag", False)]
    assert calendars[0].id == f"{PART}:443/123/calendars/home/"
    assert icloud.call_count == 3


async def test_read_events(hass: HomeAssistant, icloud) -> None:
    provider = CalDavProvider(hass, BASE, "me@icloud.com", "app-pass")
    (cal, _bday) = await provider.async_list_calendars()
    events = await provider.async_get_events(
        cal.id,
        datetime(2026, 11, 1, tzinfo=timezone.utc),
        datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    assert [(e.uid, e.summary) for e in events] == [("e1", "Arzt & Co")]
    # The object URL is remembered for edits (iCloud rejects UID searches).
    assert provider._hrefs[(cal.id, "e1")] == f"{PART}:443/123/calendars/home/ABC-1.ics"


async def test_wrong_password(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.request("PROPFIND", BASE, status=401)
    provider = CalDavProvider(hass, BASE, "me@icloud.com", "wrong")
    with pytest.raises(ProviderAuthError):
        await provider.async_list_calendars()


async def test_timeout_is_provider_error(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.request("PROPFIND", BASE, exc=TimeoutError())
    provider = CalDavProvider(hass, BASE, "me@icloud.com", "app-pass")
    with pytest.raises(ProviderError, match="no answer"):
        await provider.async_list_calendars()
