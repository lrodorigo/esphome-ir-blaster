import asyncio
import logging
from binascii import hexlify
import re

import aioesphomeapi
from aioesphomeapi import BluetoothLEAdvertisement

WRITE_CHARACTERISTIC_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"
logging.basicConfig(level=logging.INFO, datefmt='%Y-%m-%d %H:%M:%S')
logging.getLogger("aioesphomeapi").setLevel(logging.WARNING)


class RadiatorValve:

    def __init__(self, mac_address: str, cli: aioesphomeapi.APIClient):
        self.cli = cli
        self.mac_address_int = RadiatorValve.mac_to_int(mac_address)
        self.mac_str = mac_address
        self.max_tries = 5
        self.log = logging.getLogger("radiator-valve")

        self.current_packet_number = 0
        self.current_comfort_temp = 0
        self.current_resp = []
        self.received_response_event = asyncio.Event()
        self.got_response_for_current_temp = False
        self.got_packet_number = False
        self.current_mode = 0
        self.attempt_delay = 6
        self.expected_length = 0

        self.reset()

    def on_ble_state(self, connected: bool, mtu: int, error: int) -> None:
        # print(connected)
        pass

    def reset(self):
        self.current_packet_number = 0
        self.current_comfort_temp = 0
        self.current_resp = []
        self.received_response_event = asyncio.Event()
        self.got_response_for_current_temp = False
        self.got_packet_number = False
        self.current_mode = 0
        self.attempt_delay = 6
        self.expected_length = 0

    async def receive_event_wait(self, timeout=10):
        await self.received_response_event.wait()
        out = self.received_response_event.is_set()
        self.received_response_event.clear()
        return out

    async def ble_send_and_wait_response(self,
                                         function_byte: int,
                                         payload: bytearray = None,
                                         response_timeout: int = 10):
        if payload is None:
            payload = bytearray()

        to_send = [
            0xAA, 0xAA,  # start sequence
            0x00  # placeholder for size
        ]

        to_send.extend([function_byte, 0x00,0x00])

        self.current_packet_number += 1
        to_send.append(self.current_packet_number)
        to_send.extend(payload)  # FIXME: 0xAA Bytestuffing required here
        to_send[2] = len(to_send)

        checksum = self.calculate_checksum(to_send)
        to_send.append(checksum)

        self.log.debug(f"Sending pkt number {self.current_packet_number} - {hexlify(bytes(to_send))}")

        await self.cli.bluetooth_gatt_write(address=self.mac_address_int,
                                            handle=46,
                                            data=bytes(to_send),
                                            response=True, timeout=10)

        ret = await self.receive_event_wait(response_timeout)
        return ret

    async def sync_packet_number(self):
        SYNC_PACKET_FUNCTION_CODE = 0x01
        _try = 0
        while _try < 10:
            _try += 1
            self.log.info(f"[{self.mac_str}] Trying to get pkt number {_try}/10")
            ret = await self.ble_send_and_wait_response(SYNC_PACKET_FUNCTION_CODE)
            if ret:
                if self.got_packet_number:
                    self.log.info(f"[{self.mac_str}] Got Packet Number = {self.current_packet_number}")
                    return
                else:
                    self.log.debug(f"[{self.mac_str}] Packet Number Mismatch - increasing and retrying")

            await asyncio.sleep(0.5)

        raise TimeoutError("Error while trying to sync packet number / max tries exceeded")

    async def read_current_temperature(self):
        READ_TEMPERATURE_FUNCTION_CODE = 0x0C
        self.log.info(f"[{self.mac_str}] Trying to read current temperature")
        ret = await self.ble_send_and_wait_response(READ_TEMPERATURE_FUNCTION_CODE)
        if ret:
            self.got_response_for_current_temp = True
            self.log.info(f"[{self.mac_str}] Current mode = {self.current_mode}\n"
                          f" current_packet_number {self.current_packet_number}\n"
                          f" current comfort temp {self.current_comfort_temp / 10} °C")
        else:
            raise RuntimeError("Error while trying to read current temperature / max tries exceeded")

    async def write_comfort_mode(self):
        COMFORT_MODE = 0x01
        self.log.info(f"[{self.mac_str}] Trying to write comfort mode")
        SET_KEY_LOCK = 0x01
        msg = [COMFORT_MODE,  # 1 mode
               0x00,  # 2 - reserved
               0x00,  # 3 host computer op. sign
               0x00,  # 4 window sign
               0x00,  # 5 first heating mark
               0x00,  # 6 unfreeze mode previous mode
               0x00,  # 7 on off timer
               SET_KEY_LOCK,  # 8 lock flag
               0x00,
               0x00,
               0x00,
               self.current_mode]
        await self.ble_send_and_wait_response(function_byte=0x01, payload=bytearray(msg))

    async def write_open_closed(self, desired_state):

        if desired_state:
            low = (350 % 256) & 255
            high = (int(350 / 256)) & 255
            self.log.info(f"[{self.mac_str}] Setting temperature to 35 °C")
        else:
            low = 70 & 255
            high = 0x00
            self.log.info(f"[{self.mac_str}] Setting temperature to 7 °C")

        # set comfort temperature
        msg = bytearray([low, high, low, high, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        await self.ble_send_and_wait_response(function_byte=0x0C, payload=msg)

    async def set_state(self, desired_state):
        try_number = 0
        self.reset()

        while try_number < self.max_tries:

            self.log.info(f"[{self.mac_str}]  set_state [{try_number}/{self.max_tries}]")
            try_number += 1
            try:
                self.current_packet_number = 1

                await self.cli.bluetooth_device_connect(self.mac_address_int,
                                                        self.on_ble_state,
                                                        timeout=30,
                                                        disconnect_timeout=10,
                                                        address_type=0
                                                        )

                await self.cli.bluetooth_gatt_start_notify(self.mac_address_int,
                                                           handle=48,
                                                           on_bluetooth_gatt_notify=
                                                           lambda size, array: self.on_bluetooth_gatt_notify(size,
                                                                                                             array))

                await self.sync_packet_number()
                await asyncio.sleep(0.1)
                await self.read_current_temperature()
                await asyncio.sleep(0.1)
                await self.write_comfort_mode()
                await asyncio.sleep(0.1)
                await self.write_open_closed(desired_state)
                await asyncio.sleep(0.1)
                await self.cli.bluetooth_device_disconnect(self.mac_address_int)
                await asyncio.sleep(0.1)

                self.log.info(f"[{self.mac_str}]  Operation Complete")

                return True

            except Exception as e:
                self.log.exception(f"Exception in set_state (attempt: {try_number}/{self.max_tries})")
                await asyncio.sleep(self.attempt_delay)
                continue

    def set_mode(self, mode):
        self.current_mode = mode

    def set_current_packet_number(self, pkt_number):
        self.current_packet_number = pkt_number

    def set_current_comfort_temperature(self, current_comfort_temp):
        self.current_comfort_temp = current_comfort_temp

    def on_bluetooth_gatt_notify(self, size_array, value):

        self.log.debug(f"Received BLE packet: {hexlify(value)}")

        response_length = len(value)

        if self.current_resp == [] and response_length < 3:
            self.log.error("Bad packet received (unexpected length)")
            return

        if len(self.current_resp) == 0:
            if value[0] == 0xAA and value[1] == 0xAA:
                self.current_resp.extend(value)
                self.expected_length = value[2]
                # val_cut = value[3:]
                # if 0x55 in val_cut:
                #     self.log.debug("Not parsing packet")
                #     self.current_resp = []
                #     return
        else:
            self.current_resp.extend(value)

        # self.received_response_event.set()
        if len(self.current_resp) < self.expected_length:
            return

        received_checksum = self.current_resp[response_length - 1]
        expected_checksum = self.calculate_checksum(self.current_resp[0:response_length - 1])
        self.log.debug("-- Checksums --\n"
                       f"Received: {received_checksum}\n"
                       f"Expected: {expected_checksum}\n"
                       f"-- Pkt. Number --\n"
                       f"Received: {self.current_resp[6]}\n"
                       f"Expected: {self.current_packet_number}\n")

        final = self.current_resp[0:3]

        cut = [i for i in self.current_resp[3:] if i != 0x55]

        final.extend(cut)

        current_resp = final
        if expected_checksum != received_checksum:
            self.log.error(f"[{self.mac_str}] Bad Checksum")

        if current_resp[3] != 255 and current_resp[4] != 255:
            # se è un pacchetto di risposta a un comando 0x01, 0x00, 0x00 (first group)
            if current_resp[3] == 0x01 and current_resp[4] == 0x00 and current_resp[5] == 0x00:
                self.set_mode(current_resp[response_length - 2])
                self.set_current_packet_number(current_resp[6])
                self.got_packet_number = True

            if not self.got_response_for_current_temp and len(current_resp) >= 9 and current_resp[
                6] == self.current_packet_number - 1 and \
                    current_resp[3] == 0x0C and current_resp[4] == 0x00 and current_resp[5] == 0x00:
                current_comfort_temp = current_resp[8] * 256 + current_resp[7]
                self.set_current_comfort_temperature(current_comfort_temp)
                self.set_current_packet_number(current_resp[6])
                self.received_response_event.set()
        else:
            self.log.error(f"[{self.mac_str}] Bad Data Received")

        self.received_response_event.set()

        self.current_resp = []

    @staticmethod
    def calculate_checksum(msg):
        msg_to_analyze = msg[3:]
        msg_to_analyze = [i for i in msg_to_analyze if i != 0x55]
        # print(msg_to_analyze)
        checksum = (sum(msg_to_analyze) & 0xFF)
        # print(checksum)
        return checksum

    @staticmethod
    def mac_to_int(mac):
        res = re.match('^((?:(?:[0-9a-f]{2}):){5}[0-9a-f]{2})$', mac.lower())
        if res is None:
            raise ValueError('invalid mac address')
        return int(res.group(0).replace(':', ''), 16)

    def get_next_packet_number(self):
        return self.current_packet_number


async def main():
    cli = aioesphomeapi.APIClient("192.168.1.222",
                                  6053,
                                  None)
    await cli.connect(login=True)

    def cb(adv: BluetoothLEAdvertisement):
        # print(f"{adv.address} - {adv.manufacturer_data}")
        pass

    cli.subscribe_bluetooth_le_advertisements(cb)
    await asyncio.sleep(1)

    r = RadiatorValve("62:00:A1:1E:C1:1F", cli)
    await r.set_state(False)
    await asyncio.sleep(15)
    await r.set_state(True)
    print("Set state False")


if __name__ == '__main__':
    asyncio.run(main())
