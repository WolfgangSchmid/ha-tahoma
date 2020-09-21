"""The TaHoma integration."""
import asyncio
from collections import defaultdict
from datetime import timedelta
import logging

from aiohttp import CookieJar
from pyhoma.client import TahomaClient
from pyhoma.exceptions import BadCredentialsException, TooManyRequestsException
from pyhoma.models import Command
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.scene import DOMAIN as SCENE
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_EXCLUDE,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_START,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_REFRESH_STATE_INTERVAL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_REFRESH_STATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    IGNORED_TAHOMA_TYPES,
    TAHOMA_TYPES,
)
from .coordinator import TahomaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_EXECUTE_COMMAND = "execute_command"
SERVICE_REFRESH_STATES = "refresh_states"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.deprecated(CONF_EXCLUDE),
            vol.Schema(
                {
                    vol.Required(CONF_USERNAME): cv.string,
                    vol.Required(CONF_PASSWORD): cv.string,
                    vol.Optional(CONF_EXCLUDE, default=[]): vol.All(
                        cv.ensure_list, [cv.string]
                    ),
                }
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the TaHoma component."""
    configuration = config.get(DOMAIN)

    if configuration is None:
        return True

    if any(
        configuration.get(CONF_USERNAME) in entry.data.get(CONF_USERNAME)
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=configuration,
        )
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up TaHoma from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)

    session = aiohttp_client.async_create_clientsession(hass)
    client = TahomaClient(username, password, session=session)

    try:
        await client.login()
    except TooManyRequestsException:
        _LOGGER.error("too_many_requests")
        return False
    except BadCredentialsException:
        _LOGGER.error("invalid_auth")
        return False
    except Exception as exception:  # pylint: disable=broad-except
        _LOGGER.exception(exception)
        return False

    update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    tahoma_coordinator = TahomaDataUpdateCoordinator(
        hass,
        _LOGGER,
        name="TaHoma Event Fetcher",
        client=client,
        devices=await client.get_devices(),
        update_interval=timedelta(seconds=update_interval),
    )

    await tahoma_coordinator.async_refresh()

    entities = defaultdict(list)
    entities[SCENE] = await client.get_scenarios()

    for device in tahoma_coordinator.data.values():
        platform = TAHOMA_TYPES.get(device.widget) or TAHOMA_TYPES.get(device.ui_class)
        if platform:
            entities[platform].append(device)
        elif (
            device.widget not in IGNORED_TAHOMA_TYPES
            and device.ui_class not in IGNORED_TAHOMA_TYPES
        ):
            _LOGGER.debug(
                "Unsupported TaHoma device detected (%s - %s - %s)",
                device.controllable_name,
                device.ui_class,
                device.widget,
            )

    for platform in entities:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, platform)
        )

    async def handle_execute_command(call):
        """Handle execute command service."""
        entity_registry = await hass.helpers.entity_registry.async_get_registry()
        entity = entity_registry.entities.get(call.data.get("entity_id"))
        await tahoma_coordinator.client.execute_command(
            entity.unique_id,
            Command(call.data.get("command"), call.data.get("args")),
            "Home Assistant Service",
        )

    async def handle_refresh_states(*_):
        """Request a state refresh and notify DataUpdateCoordinator."""
        tahoma_coordinator.set_refresh_in_progress(True)
        tahoma_coordinator.set_update_interval(1)
        await client.refresh_states()
        await tahoma_coordinator.async_refresh()

    def _register_services(event):
        """Register the domain services."""
        hass.services.async_register(
            DOMAIN, SERVICE_REFRESH_STATES, handle_refresh_states
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_EXECUTE_COMMAND,
            handle_execute_command,
            vol.Schema(
                {
                    vol.Required("entity_id"): cv.string,
                    vol.Required("command"): cv.string,
                    vol.Optional("args", default=[]): vol.All(
                        cv.ensure_list, [vol.Any(str, int)]
                    ),
                }
            ),
        )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _register_services)

    refresh_state_interval = entry.options.get(
        CONF_REFRESH_STATE_INTERVAL, DEFAULT_REFRESH_STATE_INTERVAL
    )
    task_refresh_state = async_track_time_interval(
        hass, handle_refresh_states, timedelta(seconds=refresh_state_interval)
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "entities": entities,
        "coordinator": tahoma_coordinator,
        "update_listener": entry.add_update_listener(update_listener),
        "task_refresh_state": task_refresh_state,
    }

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""

    hass.data[DOMAIN][entry.entry_id]["update_listener"]()
    entities_per_platform = hass.data[DOMAIN][entry.entry_id]["entities"]

    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in entities_per_platform
            ]
        )
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Update when config_entry options update."""

    async def handle_refresh_states(*_):
        """Request a state refresh and notify DataUpdateCoordinator."""
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

        coordinator.set_refresh_in_progress(True)
        coordinator.set_update_interval(1)
        await coordinator.client.refresh_states()
        await coordinator.async_refresh()

    if entry.options[CONF_UPDATE_INTERVAL]:
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        new_update_interval = timedelta(seconds=entry.options[CONF_UPDATE_INTERVAL])
        coordinator.update_interval = new_update_interval
        coordinator.original_update_interval = new_update_interval

        await coordinator.async_refresh()

    if entry.options[CONF_REFRESH_STATE_INTERVAL]:
        # Cancel current task
        hass.data[DOMAIN][entry.entry_id]["task_refresh_state"]()

        # Create new task, with new time interval
        hass.data[DOMAIN][entry.entry_id][
            "task_refresh_state"
        ] = async_track_time_interval(
            hass,
            handle_refresh_states,
            timedelta(seconds=entry.options[CONF_REFRESH_STATE_INTERVAL]),
        )
