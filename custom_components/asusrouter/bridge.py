"""AsusRouter bridge module."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable, Optional

import aiohttp
from asusrouter import AsusRouter
from asusrouter.error import AsusRouterError
from asusrouter.modules.aimesh import AiMeshDevice
from asusrouter.modules.client import AsusClient
from asusrouter.modules.data import AsusData
from asusrouter.modules.homeassistant import (
    convert_to_ha_sensors,
    convert_to_ha_state_bool,
)
from asusrouter.modules.identity import AsusDevice
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed

from . import helpers
from .const import (
    BOOTTIME,
    CONF_CACHE_TIME,
    CONF_DEFAULT_CACHE_TIME,
    CONF_DEFAULT_MODE,
    CONF_DEFAULT_PORT,
    CONF_MODE,
    CPU,
    DEFAULT_SENSORS,
    FIRMWARE,
    GWLAN,
    LED,
    LIST,
    LIST_PORTS,
    METHOD,
    MODE_SENSORS,
    NETWORK,
    NETWORK_STAT,
    PARENTAL_CONTROL,
    PORT_FORWARDING,
    PORTS,
    RAM,
    SENSORS,
    SENSORS_BOOTTIME,
    SENSORS_FIRMWARE,
    SENSORS_LED,
    SENSORS_PARENTAL_CONTROL,
    SENSORS_PORT_FORWARDING,
    SENSORS_RAM,
    SENSORS_SYSINFO,
    SENSORS_WAN,
    STATE,
    SYSINFO,
    TEMPERATURE,
    VPN,
    WAN,
    WLAN,
)

_LOGGER = logging.getLogger(__name__)


class ARBridge:
    """Bridge to the AsusRouter library."""

    def __init__(
        self,
        hass: HomeAssistant,
        configs: dict[str, Any],
        options: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialize bridge to the library."""

        self.hass = hass

        # Save all the HA configs and options
        self._configs = configs.copy()
        if options:
            self._configs.update(options)

        # Get session from HA
        session = async_get_clientsession(hass)

        # Initialize API
        self._api = self._get_api(self._configs, session)

        self._host = self._configs[CONF_HOST]
        self._identity: Optional[AsusDevice] = None

        self._active: bool = False

    @staticmethod
    def _get_api(configs: dict[str, Any], session: aiohttp.ClientSession) -> AsusRouter:
        """Get AsusRouter API."""

        return AsusRouter(
            hostname=configs[CONF_HOST],
            username=configs[CONF_USERNAME],
            password=configs[CONF_PASSWORD],
            port=configs.get(CONF_PORT, CONF_DEFAULT_PORT),
            use_ssl=configs[CONF_SSL],
            cache_time=configs.get(CONF_CACHE_TIME, CONF_DEFAULT_CACHE_TIME),
            session=session,
        )

    @property
    def active(self) -> bool:
        """Return activity state of the bridge."""

        return self._active

    @property
    def api(self) -> AsusRouter:
        """Return API."""

        return self._api

    @property
    def connected(self) -> bool:
        """Return connection state."""

        return self.api.connected

    @property
    def identity(self) -> Optional[AsusDevice]:
        """Return device identity."""

        return self._identity

    # --------------------
    # Connection -->
    # --------------------

    async def async_connect(self) -> None:
        """Connect to the device."""

        _LOGGER.debug("Connecting to the API")

        await self.api.async_connect()
        self._identity = await self.api.async_get_identity()
        self._active = True

    async def async_disconnect(self) -> None:
        """Disconnect from the device."""

        _LOGGER.debug("Disconnecting from the API")

        await self.api.async_disconnect()
        self._active = False

    async def async_clean(self) -> None:
        """Cleanup."""

        _LOGGER.debug("Cleaning up")

        await self.api.async_cleanup()

    # --------------------
    # <-- Connection
    # --------------------

    async def async_cleanup_sensors(self, sensors: dict[str, Any]) -> dict[str, Any]:
        """Cleanup sensors depending on the device mode."""

        mode = self._configs.get(CONF_MODE, CONF_DEFAULT_MODE)
        available = MODE_SENSORS[mode]
        _LOGGER.debug("Available sensors for mode=`%s`: %s", mode, available)
        sensors = {
            group: details for group, details in sensors.items() if group in available
        }

        return sensors

    async def async_get_available_sensors(self) -> dict[str, dict[str, Any]]:
        """Get available sensors."""

        sensors = {
            BOOTTIME: {SENSORS: SENSORS_BOOTTIME, METHOD: self._get_data_boottime},
            CPU: {
                SENSORS: await self._get_sensors_cpu(),
                METHOD: self._get_data_cpu,
            },
            FIRMWARE: {
                SENSORS: SENSORS_FIRMWARE,
                METHOD: self._get_data_firmware,
            },
            GWLAN: {
                SENSORS: await self._get_sensors_gwlan(),
                METHOD: self._get_data_gwlan,
            },
            LED: {
                SENSORS: SENSORS_LED,
                METHOD: self._get_data_led,
            },
            NETWORK: {
                SENSORS: await self._get_sensors_network(),
                METHOD: self._get_data_network,
            },
            PARENTAL_CONTROL: {
                SENSORS: SENSORS_PARENTAL_CONTROL,
                METHOD: self._get_data_parental_control,
            },
            PORT_FORWARDING: {
                SENSORS: SENSORS_PORT_FORWARDING,
                METHOD: self._get_data_port_forwarding,
            },
            PORTS: {
                SENSORS: await self._get_sensors_ports(),
                METHOD: self._get_data_ports,
            },
            RAM: {SENSORS: SENSORS_RAM, METHOD: self._get_data_ram},
            SYSINFO: {
                SENSORS: await self._get_sensors_sysinfo(),
                METHOD: self._get_data_sysinfo,
            },
            TEMPERATURE: {
                SENSORS: await self._get_sensors_temperature(),
                METHOD: self._get_data_temperature,
            },
            VPN: {
                SENSORS: await self._get_sensors_vpn(),
                METHOD: self._get_data_vpn,
            },
            WAN: {
                SENSORS: SENSORS_WAN,
                METHOD: self._get_data_wan,
            },
            WLAN: {
                SENSORS: await self._get_sensors_wlan(),
                METHOD: self._get_data_wlan,
            },
        }

        # Cleanup sensors if needed
        sensors = await self.async_cleanup_sensors(sensors)

        return sensors

    # GET DATA FROM DEVICE ->
    # General method
    async def _get_data(
        self,
        datatype: AsusData,
        process: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Get data from the device. This is a generic method."""

        try:
            raw = await self.api.async_get_data(datatype)
            if process is not None:
                return process(raw)
            return self._process_data(raw)
        except AsusRouterError as ex:
            raise UpdateFailed(ex) from ex

    # AiMesh nodes
    async def async_get_aimesh_nodes(self) -> dict[str, AiMeshDevice]:
        """Get dict of AiMesh nodes."""

        return await self._get_data(AsusData.AIMESH)

    # Connected devices
    async def async_get_clients(self) -> dict[str, AsusClient]:
        """Get clients."""

        return await self._get_data(AsusData.CLIENTS)

    # Sensor-specific methods
    async def _get_data_boottime(self) -> dict[str, Any]:
        """Get `boottime` data from the device."""

        return await self._get_data(AsusData.BOOTTIME)

    async def _get_data_cpu(self) -> dict[str, Any]:
        """Get CPU data from the device."""

        return await self._get_data(AsusData.CPU)

    async def _get_data_firmware(self) -> dict[str, Any]:
        """Get firmware data from the device."""

        return await self._get_data(AsusData.FIRMWARE)

    async def _get_data_gwlan(self) -> dict[str, Any]:
        """Get GWLAN data from the device."""

        return await self._get_data(AsusData.GWLAN)

    async def _get_data_led(self) -> dict[str, Any]:
        """Get light data from the device."""

        return await self._get_data(AsusData.LED)

    async def _get_data_network(self) -> dict[str, Any]:
        """Get network data from device."""

        return await self._get_data(AsusData.NETWORK)

    async def _get_data_parental_control(self) -> dict[str, dict[str, int]]:
        """Get parental control data from the device."""

        return await self._get_data(
            AsusData.PARENTAL_CONTROL,
            self._process_data_parental_control,
        )

    async def _get_data_port_forwarding(self) -> dict[str, Any]:
        """Get port forwarding data from the device."""

        return await self._get_data(
            AsusData.PORT_FORWARDING,
            self._process_data_port_forwarding,
        )

    async def _get_data_ports(self) -> dict[str, dict[str, int]]:
        """Get ports data from the device."""

        return await self._get_data(AsusData.PORTS, self._process_data_ports)

    async def _get_data_ram(self) -> dict[str, Any]:
        """Get RAM data from the device."""

        return await self._get_data(AsusData.RAM)

    async def _get_data_sysinfo(self) -> dict[str, Any]:
        """Get sysinfo data from the device."""

        return await self._get_data(AsusData.SYSINFO)

    async def _get_data_temperature(self) -> dict[str, Any]:
        """Get temperarture data from the device."""

        return await self._get_data(AsusData.TEMPERATURE)

    async def _get_data_vpn(self) -> dict[str, Any]:
        """Get VPN data from the device."""

        return await self._get_data(AsusData.OPENVPN)

    async def _get_data_wan(self) -> dict[str, Any]:
        """Get WAN data from the device."""

        return await self._get_data(AsusData.WAN)

    async def _get_data_wlan(self) -> dict[str, Any]:
        """Get WLAN data from the device."""

        return await self._get_data(AsusData.WLAN)

    # <- GET DATA FROM DEVICE

    # PROCESS DATA ->
    @staticmethod
    def _process_data(raw: dict[str, Any]) -> dict[str, Any]:
        """Process data received from the device. This is a generic method."""

        return helpers.as_dict(helpers.flatten_dict(raw))

    @staticmethod
    def _process_data_parental_control(raw: dict[str, Any]) -> dict[str, Any]:
        """Process `parental control` data."""

        data: dict[str, Any] = {}
        data[STATE] = convert_to_ha_state_bool(raw.get(STATE))
        devices = []
        for rule in raw["rules"]:
            device = dataclasses.asdict(raw["rules"][rule])
            device.pop("timemap")
            devices.append(device)
        data[LIST] = devices.copy()
        return data

    @staticmethod
    def _process_data_port_forwarding(raw: dict[str, Any]) -> dict[str, Any]:
        """Process `port forwarding` data."""

        data: dict[str, Any] = {}
        data[STATE] = convert_to_ha_state_bool(raw.get(STATE))
        devices = []
        for rule in raw["rules"]:
            device = dataclasses.asdict(rule)
            devices.append(device)
        data[LIST] = devices.copy()
        return data

    @staticmethod
    def _process_data_ports(raw: dict[str, Any]) -> dict[str, Any]:
        """Process `ports` data."""

        data: dict[str, Any] = {}

        for port_type in LIST_PORTS:
            # Mark port type as disconnected
            data[port_type] = False
            # Skip if no data is provided from API
            if port_type not in raw:
                continue
            # Create ports list
            data[f"{port_type}_{LIST}"] = {}
            ports_by_type: dict[int, dict[str, Any]] = raw[port_type]
            for port_number, port_description in ports_by_type.items():
                # Mark port type connected
                if port_description.get(STATE):
                    data[port_type] = True
                # Copy port data to the list
                data[f"{port_type}_{LIST}"][port_number] = port_description

        return data

    # <- PROCESS DATA

    # GET SENSORS LIST ->
    async def _get_sensors(
        self,
        datatype: AsusData,
        process: Callable[[dict[str, Any]], list[str]] | None = None,
        sensor_type: str | None = None,
        defaults: bool = False,
    ) -> list[str]:
        """Get the available sensors. This is a generic method."""

        sensors = []
        try:
            data = await self.api.async_get_data(datatype)
            _LOGGER.debug(
                "Raw `%s` sensors of type (%s): %s", datatype, type(data), data
            )
            sensors = (
                process(data) if process is not None else self._process_sensors(data)
            )
            _LOGGER.debug("Available `%s` sensors: %s", sensor_type, sensors)
        except AsusRouterError as ex:
            if sensor_type in DEFAULT_SENSORS and defaults:
                sensors = DEFAULT_SENSORS[sensor_type]
            _LOGGER.debug(
                "Cannot get available `%s` sensors with exception: %s. \
                    Will use the following list: {sensors}",
                sensor_type,
                ex,
            )
        return sensors

    async def _get_sensors_cpu(self) -> list[str]:
        """Get the available CPU sensors."""

        return await self._get_sensors(
            AsusData.CPU,
            self._process_sensors_cpu,
            sensor_type=CPU,
            defaults=True,
        )

    async def _get_sensors_gwlan(self) -> list[str]:
        """Get the available GWLAN sensors."""

        return await self._get_sensors(
            AsusData.GWLAN,
            sensor_type=GWLAN,
        )

    async def _get_sensors_network(self) -> list[str]:
        """Get the available network stat sensors."""

        return await self._get_sensors(
            AsusData.NETWORK,
            self._process_sensors_network,
            sensor_type=NETWORK_STAT,
        )

    async def _get_sensors_ports(self) -> list[str]:
        """Get the available ports sensors."""

        return await self._get_sensors(
            AsusData.PORTS,
            self._process_sensors_ports,
            sensor_type=PORTS,
        )

    async def _get_sensors_sysinfo(self) -> list[str]:
        """Get the available sysinfo sensors."""

        return await self._get_sensors(
            AsusData.SYSINFO,
            self._process_sensors_sysinfo,
            sensor_type=SYSINFO,
        )

    async def _get_sensors_temperature(self) -> list[str]:
        """Get the available temperature sensors."""

        return await self._get_sensors(AsusData.TEMPERATURE, sensor_type=TEMPERATURE)

    async def _get_sensors_vpn(self) -> list[str]:
        """Get the available VPN sensors."""

        return await self._get_sensors(
            AsusData.OPENVPN, self._process_sensors_vpn, sensor_type=VPN
        )

    async def _get_sensors_wlan(self) -> list[str]:
        """Get the available WLAN sensors."""

        return await self._get_sensors(
            AsusData.WLAN,
            sensor_type=WLAN,
        )

    # <- GET SENSORS LIST

    # PROCESS SENSORS LIST->
    @staticmethod
    def _process_sensors(raw: dict[str, Any]) -> list[str]:
        """Process sensors from the backend library. This is a generic method.

        For the most of sensors which are returned as nested dicts
        and only the top level keys are the one we are looking for.
        """

        flat = helpers.as_dict(helpers.flatten_dict(raw))
        return helpers.list_from_dict(flat)

    @staticmethod
    def _process_sensors_cpu(raw: dict[str, Any]) -> list[str]:
        """Process CPU sensors."""

        return convert_to_ha_sensors(raw, AsusData.CPU)

    @staticmethod
    def _process_sensors_network(raw: dict[str, Any]) -> list[str]:
        """Process network sensors."""

        return convert_to_ha_sensors(raw, AsusData.NETWORK)

    @staticmethod
    def _process_sensors_ports(raw: dict[str, Any]) -> list[str]:
        """Process ports sensors."""

        sensors = []

        for port_type in LIST_PORTS:
            sensors.append(port_type)
            sensors.append(f"{port_type}_{LIST}")

        return sensors

    @staticmethod
    def _process_sensors_sysinfo(raw: dict[str, Any]) -> list[str]:
        """Process SysInfo sensors."""

        sensors = []
        for sensor_type in SENSORS_SYSINFO:
            if sensor_type in raw:
                sensors.append(sensor_type)
        return sensors

    @staticmethod
    def _process_sensors_vpn(raw: dict[str, Any]) -> list[str]:
        """Process VPN sensors."""

        return convert_to_ha_sensors(raw, AsusData.OPENVPN)

    # <- PROCESS SENSORS LIST
