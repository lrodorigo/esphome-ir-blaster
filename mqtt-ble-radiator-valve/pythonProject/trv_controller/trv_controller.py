import asyncio
import json
import logging
import time

import aioesphomeapi
import yaml
from aioesphomeapi import ReconnectLogic, APIConnectionError

from aioesphomeapi import BluetoothLERawAdvertisementsResponse
from aiomqtt import Client

from trv_controller.radiator_valve import RadiatorValve


class RadiatorValveSwitchManager:
    DISCOVERY_PREFIX = "homeassistant"
    DEVICE_TOPIC_PREFIX = "ble_radiator_valve"

    def _start_proxies_connection_manager(self):
        for proxy in self.config["bluetooth_proxies"]:
            if not proxy.get("enabled", True):
                continue

            self.connections_manager_task_group.create_task(self._proxy_connection_manager_task(proxy),
                                                            name=f"connection-to-{proxy['hostname']}")

    def __init__(self, config: dict):
        self.log = logging.getLogger("manager")

        self.config = config
        self.exited = asyncio.Event()
        self.mqtt_client: Client | None = None
        self.connections_manager_task_group = asyncio.TaskGroup()
        self.pending_commands_task_group = asyncio.TaskGroup()
        self.proxy_api_clients: dict[str, aioesphomeapi.APIClient] = dict()
        self.proxy_api_clients_busy: dict[str, bool] = dict()
        self.valve_last_seen: dict[str, float] = dict()

        self.valves_rssi_map: dict[str, dict[str, int]] = dict()  # dict(valve_mac, dict(hostname, rssi))

        self.valves = self.config.get("radiator_valve_switches", [])

    def _valve_state_topic(self, valve: dict):
        return f"{self.DEVICE_TOPIC_PREFIX}/{valve['name']}/state"

    def _valve_command_topic(self, valve: dict):
        return f"{self.DEVICE_TOPIC_PREFIX}/{valve['name']}/set"

    def _valve_availability_topic(self, valve):
        return f"{self.DEVICE_TOPIC_PREFIX}/{valve['name']}/online"

    def _valve_attributes_topic(self, valve):
        return f"{self.DEVICE_TOPIC_PREFIX}/{valve['name']}/attributes"

    async def _publish_discovery(self, client, valve):
        device_id = f"radiator_valve_{valve['name']}"
        self.log.info(f"Publishing discovery data for {device_id}")

        payload = {
            "name": "on_of",
            "unique_id": f"{device_id}",
            "state_topic": self._valve_state_topic(valve),
            "command_topic": self._valve_command_topic(valve),
            "availability": [{"topic": self._valve_availability_topic(valve)}],
            "json_attributes_topic": self._valve_attributes_topic(valve),
            "device": {
                "identifiers": [valve['mac_address']],
                # "connections": ["mac", valve['mac_address']], # just for diag
                "name": f"Radiator Valve {valve['name']}",
            }
        }

        await client.publish(topic=f"{self.DISCOVERY_PREFIX}/valve/{device_id}/config", payload=json.dumps(payload))
        # await asyncio.sleep(1)
        # await client.publish(availability_topic, "online", qos=1, retain=True)

    async def run(self):

        async with self.connections_manager_task_group as connection_tasks:
            async with self.pending_commands_task_group as command_tasks:
                self._start_proxies_connection_manager()
                connection_tasks.create_task(self._valve_availability_monitoring_task())
                async with Client(self.config["mqtt"]["host"],
                                  self.config["mqtt"].get("port", 1883)) as client:
                    self.mqtt_client = client

                    for valve in self.valves:
                        await self._publish_discovery(client, valve)

                    await client.subscribe(f"{self.DEVICE_TOPIC_PREFIX}/+/set")
                    await client.subscribe(f"{self.DISCOVERY_PREFIX}/online")

                    async for message in client.messages:

                        if message.topic.matches(f"{self.DEVICE_TOPIC_PREFIX}/+/set"):
                            device_name = str(message.topic).split("/")[1]
                            turn_on = message.payload.decode().lower() in ["true", "1", "on", "open"]
                            await self._handle_command(device_name, turn_on)

                        elif message.topic.matches(f"{self.DEVICE_TOPIC_PREFIX}/online"):
                            for valve in self.valves:
                                await self._publish_discovery(client, valve)

    async def _handle_command(self, device_name: str, turn_on: bool):
        found_valve = next(
            (switch for switch in self.config["radiator_valve_switches"] if switch["name"] == device_name),
            None)

        if found_valve is None:
            self.log.warning(f"Received command for unknown valve: {device_name}")
            return

        async def execute_command_for_valve_task(valve: dict, turn_on: bool):
            for proxy_hostname in valve['bluetooth_proxies']:
                if proxy_hostname not in self.proxy_api_clients:
                    self.log.error(
                        f"[Valve {valve['name']} - Missing proxy with hostname: {proxy_hostname} in `bluetooth_proxies` definition.")
                    continue

                mac = valve['mac_address']

                self.log.info(
                    f"[Valve {valve['name']}] [Proxy {proxy_hostname}] Trying turning {'on' if turn_on else 'off'}")
                cli = self.proxy_api_clients[proxy_hostname]

                ble_valve = RadiatorValve(mac, cli)
                if await ble_valve.set_state(turn_on):
                    self.log.info(f"[Valve {valve['name']}] [Proxy {proxy_hostname}] Done.")
                    await self._update_ha_valve_state(valve, turn_on)
                    return True

        self.pending_commands_task_group.create_task(execute_command_for_valve_task(found_valve, turn_on))

    async def _proxy_connection_manager_task(self, proxy: dict):
        """
        This task handles the while True: connect/reconnect logic for each configured BLE proxy
        """
        hostname = proxy["hostname"]

        cli = aioesphomeapi.APIClient(proxy["hostname"],
                                      proxy.get("port", 6053),
                                      password=proxy.get("password", ""))

        def _on_ble_adv(adv: BluetoothLERawAdvertisementsResponse):
            if "vanne" not in adv.name.lower():
                return
            mac = RadiatorValve.int_to_mac(adv.address)
            valve = next((valve for valve in self.valves if valve["mac_address"].lower() == mac.lower()), None)
            if valve is None:
                return
            resend_online_states = not self._valve_is_online(mac)
            self.valve_last_seen[mac] = time.time()
            if resend_online_states:
                asyncio.get_running_loop().create_task(self._publish_online_state(valve))

            self.log.debug(f"[Proxy {hostname}] Listened beacon for {valve['name']} - Rssi: {adv.rssi} dBm")

            self.valves_rssi_map.setdefault(valve['name'], dict())
            self.valves_rssi_map[valve['name']].setdefault(hostname, adv.rssi)

            # Smooth the RSSI value
            self.valves_rssi_map[valve['name']][hostname] = 0.97 * self.valves_rssi_map[valve['name']][hostname] + 0.03 * adv.rssi

            asyncio.get_running_loop().create_task(self._publish_attributes(valve))

        async def _on_connect() -> None:
            try:
                self.log.info(f"[Proxy {hostname}] ESPHome client connected")
                cli.subscribe_bluetooth_le_advertisements(_on_ble_adv)
            except APIConnectionError as err:
                self.log.warning(f"[Proxy {hostname}] ESPHome client connection error")
                await cli.disconnect()

        async def _on_disconnect(expected_disconnect) -> None:
            """Run disconnect stuff on API disconnect."""
            self.log.info(f"[Proxy {hostname}] Disconnected - Expected: '{expected_disconnect}'")

        async def _on_connect_error(err: Exception) -> None:
            """Run disconnect stuff on API disconnect."""
            self.log.exception(f"[Proxy {hostname}] - Connection Error: ")

        self.proxy_api_clients[hostname] = cli

        try:
            reconnect_logic = ReconnectLogic(
                client=cli,
                on_connect=_on_connect,
                on_disconnect=_on_disconnect,
                zeroconf_instance=None,
                name="",
                on_connect_error=_on_connect_error,
            )

            await reconnect_logic.start()
            await self.exited.wait()
            await cli.disconnect()

            del self.proxy_api_clients[hostname]

        except Exception as e:
            self.log.exception(f"[Proxy {hostname}]  Exception in _proxy_connection_manager: ")

    async def _update_ha_valve_state(self, valve: dict, is_on: bool):
        await self.mqtt_client.publish(self._valve_state_topic(valve), "open" if is_on else "closed")

    async def _valve_availability_monitoring_task(self):
        await asyncio.sleep(300)
        for valve in self.valves:
            await self._publish_online_state(valve)

    async def _publish_online_state(self, valve: dict):
        mac = valve["mac_address"].lower()
        if self._valve_is_online(mac):
            await self.mqtt_client.publish(self._valve_availability_topic(valve), "online", retain=True)
        else:
            await self.mqtt_client.publish(self._valve_availability_topic(valve), "offline", retain=True)

    def _valve_is_online(self, mac: str) -> bool:
        return (mac in self.valve_last_seen and
                time.time() - self.valve_last_seen[mac] < 60 * 5)

    async def _publish_attributes(self, valve: dict):
        attributes_map = dict()
        for key,value in self.valves_rssi_map[valve['name']].items():
            attributes_map[f"{key} RSSI"] = str(int(value)) + " dBm"
        await self.mqtt_client.publish(self._valve_attributes_topic(valve), json.dumps(attributes_map))

async def run(config_path):
    with open(config_path, "r") as file:
        config = yaml.safe_load(file)

    manager = RadiatorValveSwitchManager(config)
    await manager.run()
