"""End to end inside Home Assistant: config flow, entity, dashboard calls, services."""

from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.components.calendar import CalendarEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.calendar_sync.const import DOMAIN
from custom_components.calendar_sync.models import CalendarInfo

from .fake_caldav import FakeCalendar
from .test_caldav_provider import SERIES

CAL = "https://caldav.example.com/cal/home/"
ENTITY = "calendar.icloud_me_example_com_home"


@pytest.fixture
def fake():
    cal = FakeCalendar()
    cal.add(SERIES)
    with (
        patch(
            "custom_components.calendar_sync.caldav_provider.CalDavProvider._list_calendars",
            return_value=[CalendarInfo(CAL, "Home")],
        ),
        patch(
            "custom_components.calendar_sync.caldav_provider.CalDavProvider._calendar",
            return_value=cal,
        ),
    ):
        yield cal


@pytest.fixture
async def entry(hass: HomeAssistant, fake, freezer: FrozenDateTimeFactory):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-11-09T12:00:00+00:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iCloud (me@example.com)",
        unique_id="caldav_me@example.com@https://caldav.icloud.com/",
        data={
            "provider": "caldav",
            "url": "https://caldav.icloud.com/",
            "username": "me@example.com",
            "password": "app-pass",
            "verify_ssl": True,
            "calendars": [CAL],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_caldav(hass: HomeAssistant, fake) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "caldav"})
    assert result["step_id"] == "caldav"
    with patch("custom_components.calendar_sync.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"url": "https://caldav.icloud.com/", "username": "me@example.com", "password": "x", "verify_ssl": True},
        )
        assert result["step_id"] == "calendars"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"calendars": [CAL]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "iCloud (me@example.com)"
    assert result["data"]["calendars"] == [CAL]


async def test_config_flow_google_needs_credentials(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "google"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"


async def test_entity_supports_all_writes(hass: HomeAssistant, entry) -> None:
    state = hass.states.get(ENTITY)
    assert state is not None, hass.states.async_entity_ids("calendar")
    features = state.attributes["supported_features"]
    for flag in (
        CalendarEntityFeature.CREATE_EVENT,
        CalendarEntityFeature.UPDATE_EVENT,
        CalendarEntityFeature.DELETE_EVENT,
    ):
        assert features & flag
    assert state.attributes["message"] == "Standup"


async def test_create_update_delete_like_dashboard(hass: HomeAssistant, entry, fake, hass_ws_client) -> None:
    await hass.services.async_call(
        "calendar",
        "create_event",
        {
            "entity_id": ENTITY,
            "summary": "Zahnarzt",
            "start_date_time": "2026-11-20 15:00:00",
            "end_date_time": "2026-11-20 16:00:00",
            "location": "Praxis",
        },
        blocking=True,
    )
    resp = await hass.services.async_call(
        DOMAIN, "list_events",
        {"entity_id": ENTITY, "start": "2026-11-19 00:00:00", "end": "2026-11-22 00:00:00"},
        blocking=True, return_response=True,
    )
    events = resp[ENTITY]["events"]
    zahnarzt = next(e for e in events if e["summary"] == "Zahnarzt")
    assert zahnarzt["location"] == "Praxis"
    assert zahnarzt["start"].startswith("2026-11-20T15:00:00+01:00")

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "calendar/event/update",
        "entity_id": ENTITY,
        "uid": zahnarzt["uid"],
        "event": {
            "summary": "Zahnarzt (verschoben)",
            "dtstart": "2026-11-21T09:00:00+01:00",
            "dtend": "2026-11-21T10:00:00+01:00",
        },
    })
    result = await client.receive_json()
    assert result["success"], result

    resp = await hass.services.async_call(
        DOMAIN, "list_events",
        {"entity_id": ENTITY, "start": "2026-11-19 00:00:00", "end": "2026-11-22 00:00:00"},
        blocking=True, return_response=True,
    )
    moved = next(e for e in resp[ENTITY]["events"] if e["uid"] == zahnarzt["uid"])
    assert moved["summary"] == "Zahnarzt (verschoben)"
    assert moved["start"].startswith("2026-11-21T09:00:00+01:00")

    await client.send_json_auto_id({
        "type": "calendar/event/delete", "entity_id": ENTITY, "uid": zahnarzt["uid"],
    })
    result = await client.receive_json()
    assert result["success"], result
    assert zahnarzt["uid"] not in fake.uids()


async def test_dashboard_delete_one_occurrence(hass: HomeAssistant, entry, fake, hass_ws_client) -> None:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "calendar/event/delete",
        "entity_id": ENTITY,
        "uid": "series-1",
        "recurrence_id": "20261111T090000Z",
    })
    result = await client.receive_json()
    assert result["success"], result
    await client.send_json_auto_id({
        "type": "calendar/event/update",
        "entity_id": ENTITY,
        "uid": "series-1",
        "recurrence_id": "20261113T090000Z",
        "recurrence_range": "THISANDFUTURE",
        "event": {
            "summary": "Neu",
            "dtstart": "2026-11-13T08:00:00+01:00",
            "dtend": "2026-11-13T08:30:00+01:00",
            "rrule": "FREQ=DAILY;COUNT=2",
        },
    })
    result = await client.receive_json()
    assert result["success"], result

    resp = await hass.services.async_call(
        DOMAIN, "list_events",
        {"entity_id": ENTITY, "start": "2026-11-09 00:00:00", "end": "2026-11-20 00:00:00"},
        blocking=True, return_response=True,
    )
    days = sorted((e["start"][:10], e["summary"]) for e in resp[ENTITY]["events"])
    assert days == [
        ("2026-11-10", "Standup"),
        ("2026-11-12", "Standup"),
        ("2026-11-13", "Neu"),
        ("2026-11-14", "Neu"),
    ]


async def test_services_update_partial_and_delete(hass: HomeAssistant, entry, fake) -> None:
    await hass.services.async_call(
        DOMAIN, "update_event",
        {"entity_id": ENTITY, "uid": "series-1", "recurrence_id": "20261112T090000Z", "summary": "Nur heute"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "delete_event",
        {"entity_id": ENTITY, "uid": "series-1", "recurrence_id": "20261114T090000Z"},
        blocking=True,
    )
    resp = await hass.services.async_call(
        DOMAIN, "list_events",
        {"entity_id": ENTITY, "start": "2026-11-09 00:00:00", "end": "2026-11-20 00:00:00"},
        blocking=True, return_response=True,
    )
    days = sorted((e["start"][:16], e["summary"]) for e in resp[ENTITY]["events"])
    assert days == [
        ("2026-11-10T10:00", "Standup"),
        ("2026-11-11T10:00", "Standup"),
        ("2026-11-12T10:00", "Nur heute"),
        ("2026-11-13T10:00", "Standup"),
    ]


async def test_unload(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_entity_kept_when_server_reports_other_host(
    hass: HomeAssistant, fake, freezer: FrozenDateTimeFactory
) -> None:
    """Calendars stored as caldav.icloud.com stay the same entity on pNN hosts."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-11-09T12:00:00+00:00")
    stored = "https://caldav.example.com:443/cal/home"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iCloud (me@example.com)",
        data={
            "provider": "caldav",
            "url": "https://caldav.icloud.com/",
            "username": "me@example.com",
            "password": "app-pass",
            "verify_ssl": True,
            "calendars": [stored],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass).async_get(ENTITY)
    assert reg is not None
    assert reg.unique_id == f"{entry.entry_id}_{stored}"
    assert hass.states.get(ENTITY).state != "unavailable"


async def test_slow_server_retries_instead_of_blocking_startup(
    hass: HomeAssistant, fake
) -> None:
    """iCloud not answering must not stall Home Assistant's startup."""
    import asyncio

    from homeassistant.config_entries import ConfigEntryState

    async def hang(*_args):
        await asyncio.sleep(3600)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iCloud (me@example.com)",
        data={
            "provider": "caldav",
            "url": "https://caldav.icloud.com/",
            "username": "me@example.com",
            "password": "app-pass",
            "verify_ssl": True,
            "calendars": [CAL],
        },
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.calendar_sync.SETUP_TIMEOUT", 0.05),
        patch(
            "custom_components.calendar_sync.caldav_provider.CalDavProvider.async_list_calendars",
            hang,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
