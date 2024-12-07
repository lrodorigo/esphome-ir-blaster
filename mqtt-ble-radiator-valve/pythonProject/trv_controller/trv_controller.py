import asyncio
import json
import logging

import yaml

from aiomqtt import Client


class RadiatorValveSwitchManager:
    DISCOVERY_PREFIX = "homeassistant"
    DEVICE_TOPIC_PREFIX = "ble_radiator_valve/"

    def __init__(self, config: dict):
        self.config = config
        self.log = logging.getLogger("manager")
        self.switches = self.config.get("radiator_valve_switches", [])

    async def _publish_discovery(self, client, switch):
        device_id = f"radiator_valve_{switch['name']}"
        self.log.info(f"Publishing discovery data for {device_id}")
        topic = f"{self.DISCOVERY_PREFIX}/switch/{device_id}/config"
        availability_topic = f"{device_id}/availability"

        payload = {
            "name": f"switch.{device_id}",
            "unique_id": f"switch_{device_id}",
            "state_topic": f"{self.DEVICE_TOPIC_PREFIX}/{device_id}/state",
            "command_topic": f"{self.DEVICE_TOPIC_PREFIX}/{device_id}/set",
            "availability": [{"topic": availability_topic}],
            "device": {
                "identifiers": [switch['mac_address']],
                "name": switch['name'],
            }
        }
        await client.publish(topic=topic, payload=json.dumps(payload))
        await asyncio.sleep(1)
        # await client.publish(availability_topic, "online", qos=1, retain=True)

    async def run(self):
        async with Client(self.config["mqtt"]["host"],
                          self.config["mqtt"].get("port", 1883)) as client:

            for switch in self.switches:
                await self._publish_discovery(client, switch)

            await client.subscribe(f"{self.DEVICE_TOPIC_PREFIX}/+/set")

            async for message in client.messages:
                device_name = str(message.topic).split("/")[1]
                turn_on = str(message.payload).lower() in ["true", "1", "on"]
                await self._handle_command(device_name, turn_on)

    async def _handle_command(self, device_name: str, turn_on: bool):
        found_switch = next(
            (switch for switch in self.config["radiator_valve_switches"] if switch["name"] == device_name),
            None)

        if found_switch is None:
            self.log.warning(f"Received command for unknown device: {device_name}")
            return

        for proxy_name in found_switch["bluetooth_proxies"]:
            result = await self._proxy_try_command(proxy_name, found_switch, turn_on)
            if result:
                return

    async def _proxy_try_command(self, proxy_name: str, switch: dict, turn_on: bool):
        found_proxy = next((proxy for proxy in self.config["bluetooth_proxies"] if proxy["name"] == proxy_name),
                           None)

        if found_proxy is None:
            self.log.warning(f"Unknown proxy hostname {proxy_name} for device {switch['name']}")
            return



async def run(config_path):
    with open(config_path, "r") as file:
        config = yaml.safe_load(file)

    manager = RadiatorValveSwitchManager(config)
    await manager.run()
