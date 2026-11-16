# Changelog

## 1.0.1 – 2026-11-16

- Fix: iCloud calendars could show "could not load events" for every calendar.
  - Selected calendars are now matched by their path, so a calendar that iCloud reports under another host (caldav.icloud.com vs. pNN-caldav.icloud.com, with or without :443) keeps its entity.
  - A single event the integration cannot read is skipped and logged instead of failing the whole calendar.
  - One failing calendar no longer makes the other calendars of the account unavailable.
  - Unexpected errors (e.g. from newer caldav versions using niquests) are reported with their cause instead of as an unknown error.
  - Requests of one account are sent one after the other, since the CalDAV connection is shared and not thread safe.

## 1.0.0 – 2026-11-13

First stable release.

- Editable calendar entities for Apple iCloud (CalDAV) and Google Calendar
- Create, edit and delete events from the Home Assistant calendar dashboard
- Recurring events: edit or delete a single occurrence or "this and all following"
- Services for automations: `calendar_sync.list_events`, `calendar_sync.update_event`, `calendar_sync.delete_event`
- Read-only calendars (e.g. holidays) are shown but not editable
- Choose calendars during setup and later in the options
- English and German translations

## 0.1.0-beta.2

- Fix: editing iCloud events failed with "412 Precondition Failed", because iCloud rejects searching events by UID. Events are now loaded by their URL.

## 0.1.0-beta.1

- First beta.
