# Changelog

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
