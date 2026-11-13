"""In-memory stand-in for a caldav.Calendar."""

from __future__ import annotations

import icalendar


class FakeObject:
    def __init__(self, store: "FakeCalendar", ical: str) -> None:
        self._store = store
        self.icalendar_instance = icalendar.Calendar.from_ical(ical)

    @property
    def uid(self) -> str:
        return str(next(iter(self.icalendar_instance.walk("VEVENT")))["UID"])

    def save(self):
        self._store.objects[self.uid] = self.icalendar_instance.to_ical().decode()
        return self

    def delete(self):
        self._store.objects.pop(self.uid)


class FakeCalendar:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.url = "https://caldav.example.com/cal/home/"
        self.name = "Home"

    def get_supported_components(self):
        return ["VEVENT"]

    def add(self, ical: str) -> None:
        obj = FakeObject(self, ical)
        self.objects[obj.uid] = ical

    def save_event(self, ical: str):
        obj = FakeObject(self, ical)
        obj.save()
        return obj

    def event_by_uid(self, uid: str):
        from caldav.lib import error

        if uid not in self.objects:
            raise error.NotFoundError(uid)
        return FakeObject(self, self.objects[uid])

    def search(self, start=None, end=None, event=True, expand=False):
        return [FakeObject(self, ical) for ical in self.objects.values()]
