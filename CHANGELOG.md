# Changelog

## 1.1.0 – 2026-11-17

- iCloud/CalDAV now uses its own small CalDAV client on Home Assistant's built-in HTTP session instead of the `caldav` library.
  - Finding the calendars takes three requests, however many calendars and reminder lists the account has. Before, it made one or more requests per calendar and could run into "Server did not answer within 45 seconds".
  - No more dependency on the `caldav` library and its HTTP stack (niquests), which changed a lot between versions.
  - Reminder lists (task-only calendars) are no longer offered, and read-only calendars such as Birthdays are detected as read-only.

## 1.0.2 – 2026-11-17

- Fix: iCloud setup failed with "Server did not answer within 45 seconds". With caldav 3.x the connection now uses plain HTTP/1.1 without HTTP/2, HTTP/3 (QUIC) and OCSP lookups, which can stall behind routers and firewalls.
- Fix: "Detected blocking call to import_module" warning; the CalDAV client is now created in the executor.

## 1.0.1 – 2026-11-16

- Fix: iCloud calendars could show "could not load events" for every calendar.
  - Selected calendars are now matched by their path, so a calendar that iCloud reports under another host (caldav.icloud.com vs. pNN-caldav.icloud.com, with or without :443) keeps its entity.
  - A single event the integration cannot read is skipped and logged instead of failing the whole calendar.
  - One failing calendar no longer makes the other calendars of the account unavailable.
  - Unexpected errors (e.g. from newer caldav versions using niquests) are reported with their cause instead of as an unknown error.
  - CalDAV requests now time out after 30 seconds. Before, an iCloud server that did not answer blocked setup until Home Assistant aborted its startup ("Bootstrap stage 2 timeout"); now setup fails and is retried later. Setup itself waits at most 45 seconds for the server.
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
