"""AsusRouter switch module."""

from __future__ import annotations

import logging
from typing import Any, Optional

from asusrouter.modules.state import AsusState
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_DEFAULT_HIDE_PASSWORDS,
    CONF_HIDE_PASSWORDS,
    PASSWORD,
    STATIC_SWITCHES,
)
from .dataclass import ARSwitchDescription
from .entity import ARBinaryEntity, async_setup_ar_entry
from .router import ARDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AsusRouter switches."""

    switches = STATIC_SWITCHES.copy()

    hide = []
    if config_entry.options.get(CONF_HIDE_PASSWORDS, CONF_DEFAULT_HIDE_PASSWORDS):
        hide.append(PASSWORD)

    await async_setup_ar_entry(
        hass, config_entry, async_add_entities, switches, ARSwitch, hide
    )


class ARSwitch(ARBinaryEntity, SwitchEntity):
    """AsusRouter switch."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        router: ARDevice,
        description: ARSwitchDescription,
    ) -> None:
        """Initialize AsusRouter switch."""

        super().__init__(coordinator, router, description)
        self.entity_description: ARSwitchDescription = description

        # State on
        self._state_on = description.state_on
        self._state_on_args = description.state_on_args
        # State off
        self._state_off = description.state_off
        self._state_off_args = description.state_off_args
        # Expect modify
        self._state_expect_modify = description.state_expect_modify

    async def async_turn_on(
        self,
        **kwargs: Any,
    ) -> None:
        """Turn on switch."""

        await self._set_state(
            state=self._state_on,
            arguments=self._state_on_args,
            expect_modify=self._state_expect_modify,
        )

    async def async_turn_off(
        self,
        **kwargs: Any,
    ) -> None:
        """Turn off switch."""

        await self._set_state(
            state=self._state_off,
            arguments=self._state_off_args,
            expect_modify=self._state_expect_modify,
        )
