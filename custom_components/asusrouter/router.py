"""AsusRouter router module."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from asusrouter.error import AsusRouterAccessError
from asusrouter.modules.connection import ConnectionType
from asusrouter.modules.identity import AsusDevice
from homeassistant.components.device_tracker import CONF_CONSIDER_HOME
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SSL,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aimesh import AiMeshNode
from .bridge import ARBridge
from .client import ARClient
from .const import (
    ACCESS_POINT,
    AIMESH,
    CONF_DEFAULT_CONSIDER_HOME,
    CONF_DEFAULT_INTERVALS,
    CONF_DEFAULT_LATEST_CONNECTED,
    CONF_DEFAULT_MODE,
    CONF_DEFAULT_PORT,
    CONF_DEFAULT_PORTS,
    CONF_DEFAULT_SCAN_INTERVAL,
    CONF_DEFAULT_SPLIT_INTERVALS,
    CONF_DEFAULT_TRACK_DEVICES,
    CONF_EVENT_NODE_CONNECTED,
    CONF_INTERVAL,
    CONF_INTERVAL_DEVICES,
    CONF_LATEST_CONNECTED,
    CONF_MODE,
    CONF_REQ_RELOAD,
    CONF_SPLIT_INTERVALS,
    CONF_TRACK_DEVICES,
    CONNECTED,
    COORDINATOR,
    DEVICES,
    DOMAIN,
    FIRMWARE,
    HTTP,
    HTTPS,
    LIST,
    MAC,
    MEDIA_BRIDGE,
    METHOD,
    NO_SSL,
    NUMBER,
    ROUTER,
    SENSORS,
    SENSORS_AIMESH,
    SENSORS_CONNECTED_DEVICES,
    SSL,
)
from .helpers import as_dict

_LOGGER = logging.getLogger(__name__)


class ARSensorHandler:
    """Handler for AsusRouter sensors."""

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: ARBridge,
        options: dict[str, Any],
    ) -> None:
        """Initialise sensor handler."""

        self.hass = hass
        self.bridge = bridge

        # Selected options
        self._options = options
        self._mode = options.get(CONF_MODE, CONF_DEFAULT_MODE)
        self._split_intervals = options.get(
            CONF_SPLIT_INTERVALS, CONF_DEFAULT_SPLIT_INTERVALS
        )

        # Sensors
        self._clients_number: int = 0
        self._clients_list: list[dict[str, Any]] = []
        self._latest_connected: datetime | None = None
        self._latest_connected_list: list[dict[str, Any]] = []
        self._aimesh_number: int = 0
        self._aimesh_list: list[dict[str, Any]] = []

    async def _get_clients(self) -> dict[str, Any]:
        """Return clients sensors."""

        return {
            SENSORS_CONNECTED_DEVICES[0]: self._clients_number,
            SENSORS_CONNECTED_DEVICES[1]: self._clients_list,
            SENSORS_CONNECTED_DEVICES[2]: self._latest_connected_list,
            SENSORS_CONNECTED_DEVICES[3]: self._latest_connected,
        }

    async def _get_aimesh(self) -> dict[str, Any]:
        """Return aimesh sensors."""

        # In router / AP / Media Bridge mode
        if self._mode in (ACCESS_POINT, MEDIA_BRIDGE, ROUTER):
            return {
                NUMBER: self._aimesh_number,
                LIST: self._aimesh_list,
            }
        return {}

    def update_clients(
        self,
        clients_number: int,
        clients_list: list[Any],
        latest_connected: Optional[datetime],
        latest_connected_list: list[Any],
    ) -> bool:
        """Update connected devices attribute."""

        if (
            self._clients_number == clients_number
            and self._clients_list == clients_list
            and self._latest_connected == latest_connected
            and self._latest_connected_list == latest_connected_list
        ):
            return False
        self._clients_number = clients_number
        self._clients_list = clients_list
        self._latest_connected = latest_connected
        self._latest_connected_list = latest_connected_list
        return True

    def update_aimesh(
        self,
        nodes_number: int,
        nodes_list: list[dict[str, Any]],
    ) -> bool:
        """Update aimesh sensors."""

        if self._aimesh_number == nodes_number and self._aimesh_list == nodes_list:
            return False

        self._aimesh_number = nodes_number
        self._aimesh_list = nodes_list
        return True

    async def get_coordinator(
        self,
        sensor_type: str,
        update_method: Optional[Callable[[], Awaitable[dict[str, Any]]]] = None,
    ) -> DataUpdateCoordinator:
        """Find coordinator for the sensor type."""

        # Should sensor be polled?
        should_poll = True

        # Sensor-specific rules
        method: Callable[[], Awaitable[dict[str, Any]]]
        if sensor_type == DEVICES:
            should_poll = False
            method = self._get_clients
        elif sensor_type == AIMESH:
            should_poll = False
            method = self._get_aimesh
        elif update_method is not None:
            method = update_method
        else:
            raise RuntimeError(f"Unknown sensor type: {sensor_type}")

        # Update interval
        update_interval: timedelta
        # Static intervals
        if sensor_type == FIRMWARE:
            update_interval = timedelta(
                seconds=self._options.get(
                    CONF_INTERVAL + sensor_type,
                    CONF_DEFAULT_INTERVALS[CONF_INTERVAL + sensor_type],
                )
            )
        # Configurable intervals
        else:
            update_interval = timedelta(
                seconds=self._options.get(
                    CONF_INTERVAL + sensor_type,
                    self._options.get(CONF_SCAN_INTERVAL, CONF_DEFAULT_SCAN_INTERVAL),
                )
                if self._options.get(CONF_SPLIT_INTERVALS, CONF_DEFAULT_SPLIT_INTERVALS)
                else self._options.get(CONF_SCAN_INTERVAL, CONF_DEFAULT_SCAN_INTERVAL)
            )

        # Coordinator
        coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            name=sensor_type,
            update_method=method,
            update_interval=update_interval if should_poll else None,
        )

        _LOGGER.debug(
            "Coordinator initialized for `%s`. Update interval: `%s`",
            sensor_type,
            update_interval,
        )

        # Update coordinator
        await coordinator.async_refresh()

        return coordinator


class ARDevice:
    """Representatiion of AsusRouter."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the object."""

        self.hass = hass
        self._config_entry = config_entry
        self._options = config_entry.options.copy()

        # Device configs
        self._conf_host: str = config_entry.data[CONF_HOST]
        self._conf_port: int = self._options[CONF_PORT]
        self._conf_name: str = "AsusRouter"

        if self._conf_port == CONF_DEFAULT_PORT:
            self._conf_port = (
                CONF_DEFAULT_PORTS[SSL]
                if self._options[CONF_SSL]
                else CONF_DEFAULT_PORTS[NO_SSL]
            )

        self._mode = self._options.get(CONF_MODE, CONF_DEFAULT_MODE)

        # Bridge & device information
        self.bridge: ARBridge = ARBridge(
            hass, dict(self._config_entry.data), self._options
        )
        self._identity: AsusDevice = AsusDevice()
        self._mac: str = ""

        # Device sensors
        self._sensor_handler: ARSensorHandler | None = None
        self._sensor_coordinator: dict[str, Any] = {}

        self._aimesh: dict[str, Any] = {}
        self._clients: dict[str, Any] = {}
        self._clients_number: int = 0
        self._clients_list: list[dict[str, Any]] = []
        self._aimesh_number: int = 0
        self._aimesh_list: list[dict[str, Any]] = []
        self._latest_connected: datetime | None = None
        self._latest_connected_list: list[dict[str, Any]] = []
        self._connect_error: bool = False

        # On-clode parameters
        self._on_close: list[Callable] = []

    async def setup(self) -> None:
        """Set up an AsusRouter."""

        _LOGGER.debug("Setting up router")

        # Connect & check connection
        try:
            await self.bridge.async_connect()
        except (OSError, AsusRouterAccessError) as ex:
            raise ConfigEntryNotReady from ex
        if not self.bridge.connected:
            raise ConfigEntryNotReady

        _LOGGER.debug("Bridge connected")

        # Write the identity
        self._identity = self.bridge.identity
        self._mac = format_mac(self._identity.mac)

        # Use device model as the default device name if name not set
        if self._identity.model is not None:
            self._conf_name = self._identity.model

        # Migrate from 0.21.x and below
        # To be removed in 0.25.0
        # Tracked entities
        entity_reg = er.async_get(self.hass)
        tracked_entries = er.async_entries_for_config_entry(
            entity_reg, self._config_entry.entry_id
        )
        for entry in tracked_entries:
            uid: str = entry.unique_id
            if DOMAIN in uid:
                new_uid = uid.replace(f"{DOMAIN}_", "")

                # Check whether UID has duplicate
                conflict_entity_id = entity_reg.async_get_entity_id(
                    entry.domain, DOMAIN, new_uid
                )
                if conflict_entity_id:
                    entity_reg.async_remove(entry.entity_id)
                    continue

                entity_reg.async_update_entity(entry.entity_id, new_unique_id=new_uid)

            if any(id_to_find in uid for id_to_find in ("lan_speed", "wan_speed")):
                entity_reg.async_remove(entry.entity_id)

        # Mode-specific
        if self._mode in (ACCESS_POINT, MEDIA_BRIDGE, ROUTER):
            # Update AiMesh
            await self.update_nodes()

            # Update devices
            await self.update_devices()
        else:
            _LOGGER.debug(
                "Device is in AiMesh node mode. Device tracking and AiMesh monitoring is disabled"
            )

        # Initialize sensor coordinators
        await self._init_sensor_coordinators()

        # On-close parameters
        self.async_on_close(
            async_track_time_interval(
                self.hass,
                self.update_all,
                timedelta(
                    seconds=self._options.get(
                        CONF_INTERVAL_DEVICES, CONF_DEFAULT_SCAN_INTERVAL
                    )
                ),
            )
        )

    async def update_all(
        self,
        now: Optional[datetime] = None,
    ) -> None:
        """Update all AsusRouter platforms."""

        if self._mode in (ACCESS_POINT, MEDIA_BRIDGE, ROUTER):
            await self.update_devices()
            await self.update_nodes()

    async def update_devices(self) -> None:
        """Update AsusRouter devices tracker."""

        if self._options.get(CONF_TRACK_DEVICES, CONF_DEFAULT_TRACK_DEVICES) is False:
            _LOGGER.debug("Device tracking is disabled")
        else:
            _LOGGER.debug("Device tracking is enabled")

        _LOGGER.debug("Updating AsusRouter device list for '%s'", self._conf_host)
        try:
            api_clients = await self.bridge.async_get_clients()
            # For Media bridge mode only leave wired devices
            if self._mode == MEDIA_BRIDGE:
                api_clients = {
                    mac: client
                    for mac, client in api_clients.items()
                    if client.connection is not None
                    and client.connection.type == ConnectionType.WIRED
                }
        except UpdateFailed as ex:
            if not self._connect_error:
                self._connect_error = True
                _LOGGER.error(
                    "Error connecting to '%s' for device update: %s",
                    self._conf_host,
                    ex,
                )
            return

        if self._connect_error:
            self._connect_error = False
            _LOGGER.info("Reconnected to '%s'", self._conf_host)

        consider_home = self._options.get(
            CONF_CONSIDER_HOME, CONF_DEFAULT_CONSIDER_HOME
        )

        # Get clients
        clients = {format_mac(mac): client for mac, client in api_clients.items()}

        # Update known clients
        for client_mac, client_state in self._clients.items():
            client_info = clients.pop(client_mac, None)
            client_state.update(
                client_info,
                consider_home,
                event_call=self.fire_event,
                connected_call=self.connected_device,
            )

        # Add new clients
        new_clients = []
        new_client = False

        for client_mac, client_info in clients.items():
            # Flag that new client is added
            new_client = True

            # Create new client and process it
            client = ARClient(client_mac)
            client.update(
                client_info,
                event_call=self.fire_event,
                connected_call=self.connected_device,
            )

            # Add client to the storage
            self._clients[client_mac] = client

            # Add client to the list of new clients
            new_clients.append(client)

        # Notify about new clients
        for client in new_clients:
            self.fire_event(
                "device_connected",
                client.identity,
            )

        # Connected clients sensor
        self._clients_number = 0
        self._clients_list = []

        for client_mac, client in self._clients.items():
            if client.state:
                self._clients_number += 1
                self._clients_list.append(client.identity)

        async_dispatcher_send(self.hass, self.signal_device_update)
        if new_client:
            async_dispatcher_send(self.hass, self.signal_device_new)
        await self._update_unpolled_sensors()

    async def update_nodes(self) -> None:
        """Update AsusRouter AiMesh nodes."""

        _LOGGER.debug("Updating AiMesh status for '%s'", self._conf_host)
        try:
            aimesh = await self.bridge.async_get_aimesh_nodes()
        except UpdateFailed as ex:
            if not self._connect_error:
                self._connect_error = True
                _LOGGER.error(
                    "Error connecting to '%s' for device update: %s",
                    self._conf_host,
                    ex,
                )
            return

        new_node = False

        # Update existing nodes
        nodes = {format_mac(mac): description for mac, description in aimesh.items()}
        for node_mac, node in self._aimesh.items():
            node_info = nodes.pop(node_mac, None)
            node.update(
                node_info,
                event_call=self.fire_event,
            )

        # Add new nodes
        new_nodes = []
        for node_mac, node_info in nodes.items():
            new_node = True
            node = AiMeshNode(node_mac)
            node.update(
                node_info,
                event_call=self.fire_event,
            )
            self._aimesh[node_mac] = node
            new_nodes.append(node)

        # Notify new nodes
        for node in new_nodes:
            self.fire_event(
                CONF_EVENT_NODE_CONNECTED,
                node.identity,
            )

        # AiMesh sensors
        self._aimesh_number = 0
        self._aimesh_list = []
        for mac, node in self._aimesh.items():
            if node.identity[CONNECTED]:
                self._aimesh_number += 1
            self._aimesh_list.append(node.identity)

        async_dispatcher_send(self.hass, self.signal_aimesh_update)
        if new_node:
            async_dispatcher_send(self.hass, self.signal_aimesh_new)

    async def _init_sensor_coordinators(self) -> None:
        """Initialize sensor coordinators."""

        # If already initialized
        if self._sensor_handler:
            return

        # Initialize sensor handler
        self._sensor_handler = ARSensorHandler(self.hass, self.bridge, self._options)

        # Update devices
        self._sensor_handler.update_clients(
            self._clients_number,
            self._clients_list,
            self._latest_connected,
            self._latest_connected_list,
        )
        self._sensor_handler.update_aimesh(
            self._aimesh_number,
            self._aimesh_list,
        )

        # Get available sensors
        available_sensors = await self.bridge.async_get_available_sensors()

        # Add devices sensors
        if self._mode in (ACCESS_POINT, MEDIA_BRIDGE, ROUTER):
            available_sensors[DEVICES] = {SENSORS: SENSORS_CONNECTED_DEVICES}
            available_sensors[AIMESH] = {SENSORS: SENSORS_AIMESH}

        # Process available sensors
        for sensor_type, sensor_definition in available_sensors.items():
            sensor_names = sensor_definition.get(SENSORS)
            if not sensor_names:
                continue

            # Find and initialize coordinator
            coordinator = await self._sensor_handler.get_coordinator(
                sensor_type, sensor_definition.get(METHOD)
            )

            # Save the coordinator
            self._sensor_coordinator[sensor_type] = {
                COORDINATOR: coordinator,
                sensor_type: sensor_names,
            }

    async def _update_unpolled_sensors(self) -> None:
        """Request refresh for AsusRouter unpolled sensors."""

        # If sensor handler is not initialized
        if not self._sensor_handler:
            return

        # AiMesh
        if AIMESH in self._sensor_coordinator:
            coordinator = self._sensor_coordinator[AIMESH][COORDINATOR]
            if self._sensor_handler.update_aimesh(
                self._aimesh_number,
                self._aimesh_list,
            ):
                await coordinator.async_refresh()

        # Devices
        if DEVICES in self._sensor_coordinator:
            coordinator = self._sensor_coordinator[DEVICES][COORDINATOR]
            if self._sensor_handler.update_clients(
                self._clients_number,
                self._clients_list,
                self._latest_connected,
                self._latest_connected_list,
            ):
                await coordinator.async_refresh()

    async def close(self) -> None:
        """Close the connection."""

        # Disconnect the bridge
        if self.bridge.active:
            await self.bridge.async_disconnect()

        # Run on-close methods
        for func in self._on_close:
            func()

        self._on_close.clear()

    @callback
    def async_on_close(
        self,
        func: CALLBACK_TYPE,
    ) -> None:
        """Functions on router close."""

        self._on_close.append(func)

    def update_options(
        self,
        new_options: dict,
    ) -> bool:
        """Update router options."""

        require_reload = False
        for name, new_option in new_options.items():
            if name in CONF_REQ_RELOAD:
                old_opt = self._options.get(name)
                if not old_opt or old_opt != new_option:
                    require_reload = True
                    break

        self._options.update(new_options)
        return require_reload

    def connected_device_time(self, element: dict[str, Any]) -> datetime:
        """Get connected time for the device."""

        connected = element.get("connected")
        if isinstance(connected, datetime):
            return connected
        return datetime.now(timezone.utc)

    @callback
    def connected_device(
        self,
        identity: dict[str, Any],
    ) -> None:
        """Mark device connected."""

        mac = identity.get(MAC, None)
        if not mac:
            return

        # If device already in list
        for device in self._latest_connected_list:
            if device.get(MAC, None) == mac:
                self._latest_connected_list.remove(device)

        # Sort the list by time
        self._latest_connected_list.sort(key=self.connected_device_time)

        # Add new identity
        self._latest_connected_list.append(identity)

        # Check the size
        while len(self._latest_connected_list) > self._options.get(
            CONF_LATEST_CONNECTED, CONF_DEFAULT_LATEST_CONNECTED
        ):
            self._latest_connected_list.pop(0)

        # Update latest connected time
        self._latest_connected = self._latest_connected_list[-1].get(CONNECTED)

    @callback
    def fire_event(
        self,
        event: str,
        args: Optional[dict[str, Any]] = None,
    ):
        """Fire HA event."""

        if self._options.get(event) is True:
            event_name = f"{DOMAIN}_{event}"
            _LOGGER.debug("Firing event: `%s`", event_name)
            self.hass.bus.fire(
                event_name,
                args,
            )

    async def remove_trackers(self, **kwargs: Any) -> None:
        """Remove device trackers."""

        _LOGGER.debug("Removing trackers")

        # Check that data is provided
        raw = kwargs.get("raw", None)
        if raw is None:
            return

        # Get entities to remove
        if "entities" in raw:
            entities = raw["entities"]
            entity_reg = er.async_get(self.hass)
            for entity in entities:
                reg_value = entity_reg.async_get(entity)
                if not isinstance(reg_value, er.RegistryEntry):
                    continue
                capabilities: dict[str, Any] = as_dict(reg_value.capabilities)
                mac = capabilities[MAC]
                _LOGGER.debug("Trying to remove tracker with mac: %s", mac)
                if mac in self._clients:
                    self._clients.pop(mac)
                    _LOGGER.debug("Found and removed")

        # Update devices
        await self.update_devices()

        # Reload device tracker platform
        unload = await self.hass.config_entries.async_unload_platforms(
            self._config_entry, [Platform.DEVICE_TRACKER]
        )
        if unload:
            self.hass.config_entries.async_setup_platforms(
                self._config_entry, [Platform.DEVICE_TRACKER]
            )

    @property
    def device_info(self) -> DeviceInfo:
        """Device information."""

        return DeviceInfo(
            identifiers={
                (DOMAIN, self.mac),
                (DOMAIN, self._identity.serial),
            },
            name=self._conf_name,
            model=self._identity.model,
            manufacturer=self._identity.brand,
            sw_version=str(self._identity.firmware),
            configuration_url=f"{HTTPS if self._options[CONF_SSL] else HTTP}://\
{self._conf_host}:{self._conf_port}",
        )

    @property
    def signal_aimesh_new(self) -> str:
        """Notify new AiMesh nodes."""

        return f"{DOMAIN}-aimesh-new"

    @property
    def signal_aimesh_update(self) -> str:
        """Notify updated AiMesh nodes."""

        return f"{DOMAIN}-aimesh-update"

    @property
    def signal_device_new(self) -> str:
        """Notify new device."""

        return f"{DOMAIN}-device-new"

    @property
    def signal_device_update(self) -> str:
        """Notify updated device."""

        return f"{DOMAIN}-device-update"

    @property
    def aimesh(self) -> dict[str, Any]:
        """Return AiMesh nodes."""

        return self._aimesh

    @property
    def devices(self) -> dict[str, Any]:
        """Return devices."""

        return self._clients

    @property
    def mac(self) -> str:
        """Router MAC address."""

        return self._mac

    @property
    def sensor_coordinator(self) -> dict[str, Any]:
        """Return sensor coordinator."""

        return self._sensor_coordinator
