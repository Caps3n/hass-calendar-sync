"""A small async CalDAV client on top of Home Assistant's aiohttp session.

Only what this integration needs: find the calendars of an account, query
events in a time range, and read, write and delete single calendar objects.
Discovery takes three requests (principal, calendar home, calendar list),
whatever the number of calendars. The `caldav` library made one or more
requests per calendar, and with many iCloud calendars and reminder lists the
setup ran into Home Assistant's startup timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from urllib.parse import unquote, urljoin, urlsplit
import uuid
from xml.etree import ElementTree as ET

import aiohttp
import icalendar

_LOGGER = logging.getLogger(__name__)

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CS = "http://calendarserver.org/ns/"
ICAL = "http://apple.com/ns/ical/"

# Seconds a single request may take before it is given up.
REQUEST_TIMEOUT = 30

_WRITE_PRIVILEGES = {"write", "write-content", "all", "bind"}


class DavError(Exception):
    """A CalDAV request failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DavAuthError(DavError):
    """The server rejected the credentials."""


class DavNotFound(DavError):
    """The resource does not exist."""


@dataclass
class DavCalendarInfo:
    url: str
    name: str
    writable: bool = True


@dataclass
class DavObject:
    """One calendar object resource (an .ics file on the server)."""

    href: str
    etag: str | None
    icalendar_instance: icalendar.Calendar

    @property
    def url(self) -> str:
        return self.href


def _tag(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


def _text(elem: ET.Element | None) -> str | None:
    if elem is None or elem.text is None:
        return None
    return elem.text.strip()


def _ical_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_ical(text: str) -> icalendar.Calendar:
    return icalendar.Calendar.from_ical(text)


class DavClient:
    """Talks CalDAV to one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
    ) -> None:
        self._session = session
        self._url = url if url.endswith("/") else url + "/"
        self._auth = aiohttp.BasicAuth(username, password)
        self._ssl = None if verify_ssl else False
        self._timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    # -- transport ---------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], str, str]:
        """Send a request; return status, headers, body text and final URL."""
        hdrs = {"User-Agent": "calendar-sync-home-assistant"}
        if headers:
            hdrs.update(headers)
        if isinstance(body, str):
            body = body.encode()
        for _ in range(5):
            try:
                async with self._session.request(
                    method,
                    url,
                    data=body,
                    headers=hdrs,
                    auth=self._auth,
                    timeout=self._timeout,
                    ssl=self._ssl,
                    allow_redirects=False,
                ) as resp:
                    text = await resp.text(errors="replace")
                    status = resp.status
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            except TimeoutError as err:
                raise DavError(
                    f"{method} {url}: no answer within {REQUEST_TIMEOUT} seconds"
                ) from err
            except aiohttp.ClientError as err:
                raise DavError(f"{method} {url}: {err}") from err
            _LOGGER.debug("%s %s -> %s", method, url, status)
            if status in (301, 302, 307, 308) and "location" in resp_headers:
                # Follow redirects by hand so method, body and credentials stay.
                url = urljoin(url, resp_headers["location"])
                continue
            if status == 401:
                raise DavAuthError(f"{method} {url}: 401 Unauthorized", status)
            if status == 404:
                raise DavNotFound(f"{method} {url}: 404 Not Found", status)
            if status >= 400:
                raise DavError(f"{method} {url}: {status} {text[:200]}", status)
            return status, resp_headers, text, url
        raise DavError(f"{method} {url}: too many redirects")

    async def _multistatus(
        self, method: str, url: str, body: str, depth: str
    ) -> tuple[list[ET.Element], str]:
        _status, _headers, text, final_url = await self.request(
            method,
            url,
            body,
            {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        try:
            root = ET.fromstring(text)
        except ET.ParseError as err:
            raise DavError(f"{method} {url}: invalid XML in answer") from err
        return root.findall(_tag(DAV, "response")), final_url

    @staticmethod
    def _ok_prop(response: ET.Element) -> ET.Element:
        """Merge the <prop> elements of all 2xx <propstat> blocks."""
        merged = ET.Element(_tag(DAV, "prop"))
        for propstat in response.findall(_tag(DAV, "propstat")):
            parts = (_text(propstat.find(_tag(DAV, "status"))) or "HTTP/1.1 200").split()
            if len(parts) > 1 and not parts[1].startswith("2"):
                continue
            prop = propstat.find(_tag(DAV, "prop"))
            if prop is not None:
                merged.extend(list(prop))
        return merged

    @staticmethod
    def _href(response: ET.Element, base: str) -> str:
        return urljoin(base, _text(response.find(_tag(DAV, "href"))) or "")

    # -- discovery ---------------------------------------------------------

    async def _find_href(self, url: str, ns: str, name: str) -> str | None:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="DAV:" xmlns:c="{CALDAV}">'
            f"<d:prop><{'c' if ns == CALDAV else 'd'}:{name}/></d:prop>"
            "</d:propfind>"
        )
        responses, final_url = await self._multistatus("PROPFIND", url, body, "0")
        for response in responses:
            href = self._ok_prop(response).find(f"{_tag(ns, name)}/{_tag(DAV, 'href')}")
            if (value := _text(href)) is not None:
                return urljoin(final_url, value)
        return None

    async def calendars(self) -> list[DavCalendarInfo]:
        """Return the event calendars of the account."""
        principal = await self._find_href(self._url, DAV, "current-user-principal")
        home = await self._find_href(principal or self._url, CALDAV, "calendar-home-set")
        home = home or principal or self._url
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="DAV:" xmlns:c="{CALDAV}">'
            "<d:prop><d:resourcetype/><d:displayname/>"
            "<c:supported-calendar-component-set/><d:current-user-privilege-set/>"
            "</d:prop></d:propfind>"
        )
        responses, final_url = await self._multistatus("PROPFIND", home, body, "1")
        result = []
        for response in responses:
            prop = self._ok_prop(response)
            rtype = prop.find(_tag(DAV, "resourcetype"))
            if rtype is None or rtype.find(_tag(CALDAV, "calendar")) is None:
                continue
            comps = prop.find(_tag(CALDAV, "supported-calendar-component-set"))
            if comps is not None and len(comps):
                names = {c.get("name", "").upper() for c in comps}
                if "VEVENT" not in names:
                    continue  # reminder lists and other task-only calendars
            writable = True
            privs = prop.find(_tag(DAV, "current-user-privilege-set"))
            if privs is not None and len(privs):
                granted = {
                    child.tag.split("}", 1)[-1]
                    for priv in privs.findall(_tag(DAV, "privilege"))
                    for child in priv
                }
                writable = bool(granted & _WRITE_PRIVILEGES)
            url = self._href(response, final_url)
            name = _text(prop.find(_tag(DAV, "displayname"))) or unquote(
                urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            )
            result.append(DavCalendarInfo(url=url, name=name, writable=writable))
        _LOGGER.debug("Found calendars: %s", result)
        return result

    # -- calendar objects --------------------------------------------------

    async def query(
        self, calendar_url: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[DavObject]:
        """Return the VEVENT objects overlapping [start, end), or all of them."""
        time_range = ""
        if start is not None and end is not None:
            time_range = f'<c:time-range start="{_ical_time(start)}" end="{_ical_time(end)}"/>'
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<c:calendar-query xmlns:d="DAV:" xmlns:c="{CALDAV}">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR">'
            f'<c:comp-filter name="VEVENT">{time_range}</c:comp-filter>'
            "</c:comp-filter></c:filter></c:calendar-query>"
        )
        responses, final_url = await self._multistatus("REPORT", calendar_url, body, "1")
        objects: list[DavObject] = []
        missing: list[str] = []
        for response in responses:
            href = self._href(response, final_url)
            if href.rstrip("/") == final_url.rstrip("/"):
                continue  # some servers list the collection itself
            prop = self._ok_prop(response)
            etag = _text(prop.find(_tag(DAV, "getetag")))
            data = prop.find(_tag(CALDAV, "calendar-data"))
            if data is None or not (data.text or "").strip():
                missing.append(href)
                continue
            try:
                objects.append(DavObject(href, etag, _parse_ical(data.text)))
            except ValueError:
                _LOGGER.warning("Skipping unreadable calendar object %s", href, exc_info=True)
        for href in missing:
            try:
                objects.append(await self.get(href))
            except DavNotFound:
                continue
        return objects

    async def get(self, href: str) -> DavObject:
        _status, headers, text, _url = await self.request("GET", href)
        try:
            return DavObject(href, headers.get("etag"), _parse_ical(text))
        except ValueError as err:
            raise DavError(f"GET {href}: not a calendar object") from err

    async def put(self, href: str, ical: str, etag: str | None = None) -> str | None:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        _status, resp_headers, _text_, _url = await self.request("PUT", href, ical, headers)
        return resp_headers.get("etag")

    async def delete(self, href: str, etag: str | None = None) -> None:
        headers = {"If-Match": etag} if etag else None
        try:
            await self.request("DELETE", href, headers=headers)
        except DavNotFound:
            pass


class DavCalendar:
    """One calendar collection; the interface the provider works with."""

    def __init__(self, client: DavClient, url: str) -> None:
        self._client = client
        self.url = url

    async def search(self, start: datetime, end: datetime) -> list[DavObject]:
        return await self._client.query(self.url, start, end)

    async def objects(self) -> list[DavObject]:
        return await self._client.query(self.url)

    async def get(self, href: str) -> DavObject:
        return await self._client.get(href)

    async def create(self, ical: icalendar.Calendar) -> DavObject:
        href = urljoin(self.url.rstrip("/") + "/", f"{uuid.uuid4()}.ics")
        etag = await self._client.put(href, ical.to_ical().decode())
        return DavObject(href, etag, ical)

    async def save(self, obj: DavObject) -> None:
        obj.etag = await self._client.put(
            obj.href, obj.icalendar_instance.to_ical().decode(), obj.etag
        )

    async def delete(self, obj: DavObject) -> None:
        await self._client.delete(obj.href, obj.etag)
