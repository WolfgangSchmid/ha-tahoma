"""Helpers to help coordinate updates."""
from datetime import timedelta
import logging
from typing import Dict, List, Optional, Union

from aiohttp import ServerDisconnectedError
from pyhoma.client import TahomaClient
from pyhoma.enums import EventName, ExecutionState
from pyhoma.exceptions import (
    BadCredentialsException,
    NotAuthenticatedException,
    TooManyRequestsException,
)
from pyhoma.models import DataType, Device, State

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

TYPES = {
    DataType.NONE: None,
    DataType.INTEGER: int,
    DataType.DATE: int,
    DataType.STRING: str,
    DataType.FLOAT: float,
    DataType.BOOLEAN: bool,
}

_LOGGER = logging.getLogger(__name__)


class TahomaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching TaHoma data."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        *,
        name: str,
        client: TahomaClient,
        devices: List[Device],
        update_interval: Optional[timedelta] = None,
    ):
        """Initialize global data updater."""
        super().__init__(
            hass, logger, name=name, update_interval=update_interval,
        )

        self.data = {}
        self.original_update_interval = update_interval
        self.client = client
        self.devices: Dict[str, Device] = {d.deviceurl: d for d in devices}
        self.executions: Dict[str, Dict[str, str]] = {}
        self.refresh_in_progress = False

    async def _async_update_data(self) -> Dict[str, Device]:
        """Fetch TaHoma data via event listener."""
        try:
            events = await self.client.fetch_events()
        except BadCredentialsException:
            raise UpdateFailed("invalid_auth")
        except TooManyRequestsException:
            raise UpdateFailed("too_many_requests")
        except (ServerDisconnectedError, NotAuthenticatedException):
            self.executions = {}
            self.set_refresh_in_progress(False)
            await self.client.login()
            self.devices = await self._get_devices()
            return self.devices
        except Exception as exception:
            _LOGGER.debug(exception)
            raise UpdateFailed(exception)

        for event in events:
            _LOGGER.debug(
                f"{event.name}/{event.exec_id} (device:{event.deviceurl},state:{event.old_state}->{event.new_state})"
            )

            if event.name == EventName.DEVICE_AVAILABLE:
                self.devices[event.deviceurl].available = True

            elif event.name in [
                EventName.DEVICE_UNAVAILABLE,
                EventName.DEVICE_DISABLED,
            ]:
                self.devices[event.deviceurl].available = False

            elif event.name in [
                EventName.DEVICE_CREATED,
                EventName.DEVICE_UPDATED,
            ]:
                self.devices = await self._get_devices()

            elif event.name == EventName.DEVICE_REMOVED:
                registry = await device_registry.async_get_registry(self.hass)
                registry.async_remove_device(event.deviceurl)
                del self.devices[event.deviceurl]

            elif event.name == EventName.DEVICE_STATE_CHANGED:
                for state in event.device_states:
                    device = self.devices[event.deviceurl]
                    if state.name not in device.states:
                        device.states[state.name] = state
                    device.states[state.name].value = self._get_state(state)

            elif event.name == EventName.EXECUTION_REGISTERED:
                if event.exec_id not in self.executions:
                    self.executions[event.exec_id] = {}
                self.set_update_interval(1)

            elif (
                event.name == EventName.EXECUTION_STATE_CHANGED
                and event.exec_id in self.executions
                and event.new_state in [ExecutionState.COMPLETED, ExecutionState.FAILED]
            ):
                del self.executions[event.exec_id]

            elif event.name == EventName.REFRESH_ALL_DEVICES_STATES_COMPLETED:
                self.set_refresh_in_progress(False)

        if not self.executions and self.refresh_in_progress is False:
            self.restore_update_interval()

        return self.devices

    async def _get_devices(self) -> Dict[str, Device]:
        """Fetch devices."""
        return {d.deviceurl: d for d in await self.client.get_devices(refresh=True)}

    def set_refresh_in_progress(self, state: bool) -> None:
        """Set refresh in progress to argument value."""
        self.refresh_in_progress = state

    def set_update_interval(self, seconds: int = 1) -> None:
        """Set update interval to argument value."""
        self.update_interval = timedelta(seconds=seconds)

    def restore_update_interval(self) -> None:
        """Restore update interval to original update interval."""
        self.update_interval = self.original_update_interval

    @staticmethod
    def _get_state(state: State) -> Union[float, int, bool, str, None]:
        """Cast string value to the right type."""
        if state.type != DataType.NONE:
            caster = TYPES.get(DataType(state.type))
            return caster(state.value)
        return state.value
