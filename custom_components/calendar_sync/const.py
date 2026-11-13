"""Constants for Calendar Sync."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "calendar_sync"

CONF_PROVIDER: Final = "provider"
CONF_CALENDARS: Final = "calendars"

PROVIDER_CALDAV: Final = "caldav"
PROVIDER_GOOGLE: Final = "google"

ICLOUD_CALDAV_URL: Final = "https://caldav.icloud.com/"

GOOGLE_API_BASE: Final = "https://www.googleapis.com/calendar/v3"
GOOGLE_AUTHORIZE_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE: Final = "https://www.googleapis.com/auth/calendar"

UPDATE_INTERVAL: Final = timedelta(minutes=5)
# Window of events kept in memory for the entity state (current/next event).
LOOKBACK: Final = timedelta(days=1)
LOOKAHEAD: Final = timedelta(days=60)

THIS_AND_FUTURE: Final = "THISANDFUTURE"

SERVICE_UPDATE_EVENT: Final = "update_event"
SERVICE_DELETE_EVENT: Final = "delete_event"
SERVICE_LIST_EVENTS: Final = "list_events"
