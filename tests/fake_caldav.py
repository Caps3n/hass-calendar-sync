"""In-memory stand-in for a DavCalendar.

Objects live at opaque URLs (not <uid>.ics) like on iCloud. There is no UID
search at all: like on iCloud, objects are found by URL or by scanning.
"""

from __future__ import annotations

import hashlib

import icalendar

from custom_components.calendar_sync.dav import DavNotFound, DavObject


class FakeCalendar:
    def __init__(self, reject_uid_search: bool = False) -> None:
        self.store: dict[str, str] = {}  # url -> ical
        self.url = "https://caldav.example.com/cal/home/"
        self.name = "Home"
        self.reject_uid_search = reject_uid_search
        self.uid_searches = 0  # kept for the tests; UID search is never used

    @staticmethod
    def _uid(ical: str) -> str:
        cal = icalendar.Calendar.from_ical(ical)
        return str(next(iter(cal.walk("VEVENT")))["UID"])

    def url_for(self, uid: str) -> str:
        return f"{self.url}{hashlib.sha1(uid.encode()).hexdigest()[:12]}.ics"

    def uids(self) -> set[str]:
        return {self._uid(ical) for ical in self.store.values()}

    def ical_for(self, uid: str) -> str:
        return next(i for i in self.store.values() if self._uid(i) == uid)

    def add(self, ical: str) -> None:
        self.store[self.url_for(self._uid(ical))] = ical

    def events(self) -> list[DavObject]:
        return [
            DavObject(url, None, icalendar.Calendar.from_ical(ical))
            for url, ical in self.store.items()
        ]

    # -- DavCalendar interface ---------------------------------------------

    async def search(self, start, end) -> list[DavObject]:
        return self.events()

    async def objects(self) -> list[DavObject]:
        return self.events()

    async def get(self, href: str) -> DavObject:
        if href not in self.store:
            raise DavNotFound(href, 404)
        return DavObject(href, None, icalendar.Calendar.from_ical(self.store[href]))

    async def create(self, ical: icalendar.Calendar) -> DavObject:
        text = ical.to_ical().decode()
        href = self.url_for(self._uid(text))
        self.store[href] = text
        return DavObject(href, None, ical)

    async def save(self, obj: DavObject) -> None:
        self.store[obj.href] = obj.icalendar_instance.to_ical().decode()

    async def delete(self, obj: DavObject) -> None:
        self.store.pop(obj.href, None)
