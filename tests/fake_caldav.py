"""In-memory stand-in for a caldav.Calendar.

Objects live at opaque URLs (not <uid>.ics) like on iCloud. With
`reject_uid_search=True` the UID search fails with 412 like iCloud does.
"""

from __future__ import annotations

import hashlib

from caldav.lib import error
import icalendar


class FakeObject:
    def __init__(self, store: "FakeCalendar", ical: str, url: str | None = None) -> None:
        self._store = store
        self.icalendar_instance = icalendar.Calendar.from_ical(ical)
        self.url = url

    @property
    def uid(self) -> str:
        return str(next(iter(self.icalendar_instance.walk("VEVENT")))["UID"])

    def save(self):
        if self.url is None:
            self.url = self._store.url_for(self.uid)
        self._store.objects[self.url] = self.icalendar_instance.to_ical().decode()
        return self

    def delete(self):
        self._store.objects.pop(self.url)


class FakeCalendar:
    def __init__(self, reject_uid_search: bool = False) -> None:
        self.objects: dict[str, str] = {}  # url -> ical
        self.url = "https://caldav.example.com/cal/home/"
        self.name = "Home"
        self.reject_uid_search = reject_uid_search
        self.uid_searches = 0

    def url_for(self, uid: str) -> str:
        return f"{self.url}{hashlib.sha1(uid.encode()).hexdigest()[:12]}.ics"

    def uids(self) -> set[str]:
        return {FakeObject(self, ical).uid for ical in self.objects.values()}

    def ical_for(self, uid: str) -> str:
        return next(i for i in self.objects.values() if FakeObject(self, i).uid == uid)

    def get_supported_components(self):
        return ["VEVENT"]

    def add(self, ical: str) -> None:
        FakeObject(self, ical).save()

    def save_event(self, ical: str):
        return FakeObject(self, ical).save()

    def event_by_url(self, href):
        if href not in self.objects:
            raise error.NotFoundError(href)
        return FakeObject(self, self.objects[href], href)

    def event_by_uid(self, uid: str):
        self.uid_searches += 1
        if self.reject_uid_search:
            raise error.ReportError("412 Precondition Failed")
        for url, ical in self.objects.items():
            obj = FakeObject(self, ical, url)
            if obj.uid == uid:
                return obj
        raise error.NotFoundError(uid)

    def events(self):
        return [FakeObject(self, ical, url) for url, ical in self.objects.items()]

    def search(self, start=None, end=None, event=True, expand=False):
        return self.events()
