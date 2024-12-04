import asyncio
import logging
import threading
from binascii import hexlify
import time
import re

import aioesphomeapi
from aioesphomeapi import BluetoothLEAdvertisement

WRITE_CHARACTERISTIC_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"
logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')

class RadiatorValve:
    class MacLogFilter(logging.Filter):
        def __init__(self, mac_address):
            super().__init__()
            self.mac_address = mac_address

        def filter(self, record):
            record.mac_address = self.mac_address
            return True

    def __init__(self, mac_address: str, cli: aioesphomeapi.APIClient):
        self.cli = cli
        self.mac_address_int = RadiatorValve.mac_to_int(mac_address)
        self.mac_str = mac_address
        self.current_mode = 0
        self.log = logging.getLogger("radiator-valve")
        self.log.addFilter(RadiatorValve.MacLogFilter(self.mac_str))

        self.current_packet_number = 1
        self.current_comfort_temp = 0
        self.current_resp = []
        self.received_response_event = threading.Event()
        self.got_packet_number = False
        self.got_response_for_current_temp = False

    def on_ble_state(self, connected: bool, mtu: int, error: int) -> None:
        # print(connected)
        pass

    async def set_state(self, desired_state):
        try:

            await self.cli.bluetooth_device_connect(self.mac_address_int,
                                                    self.on_ble_state,
                                                    timeout=30,
                                                    disconnect_timeout=10,
                                                    address_type=0
                                                    )

            await self.cli.bluetooth_gatt_start_notify(self.mac_address_int,
                                                       handle=48,
                                                       on_bluetooth_gatt_notify=
                                                       lambda size, array: self.on_bluetooth_gatt_notify(size, array))

            self.got_packet_number = False
            self.current_packet_number = 1
            while not self.got_packet_number:
                msg = bytearray([0xAA, 0xAA, 0x07, 0x01, 0x00, 0x00, self.next_packet_number()])
                checksum = self.calculate_checksum(msg)
                msg.append(checksum)

                await self.cli.bluetooth_gatt_write(address=self.mac_address_int,
                                                    handle=46,
                                                    data=bytes(msg),
                                                    response=True, timeout=10)

                ret = self.received_response_event.wait(timeout=1)

                if ret:
                    self.log.info(f"[{self.mac_str}] Got Packet Number")
                    self.got_packet_number = True

            self.received_response_event.clear()

            self.log.debug(f"[{self.mac_str}] Getting current comfort temp")
            # loop per ottenere la current comfort temp
            self.got_response_for_current_temp = False
            while not self.got_response_for_current_temp:
                msg = bytearray([0xAA, 0xAA, 0x07, 0x0C, 0x00, 0x00, self.next_packet_number()])
                checksum = self.calculate_checksum(msg)
                msg.append(checksum)
                await self.cli.bluetooth_gatt_write(address=self.mac_address_int,
                                                    handle=46,
                                                    data=bytes(msg),
                                                    response=True, timeout=3)

                ret = self.received_response_event.wait(timeout=1)
                if ret:
                    self.got_response_for_current_temp = True

            # set comfort mode
            comfort_mode = 0x01
            self.log.info(f"[{self.mac_str}] Current mode = {self.current_mode}\n"
                          f" current_packet_number {self.current_packet_number}\n"
                          f" current comfort temp {self.current_comfort_temp} °C")

            msg = bytearray([0xAA, 0xAA, 0x13, 0x01, 0x00, 0x00, self.next_packet_number(), comfort_mode, 0x00,
                             0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, self.current_mode])
            checksum = self.calculate_checksum(msg)
            msg.append(checksum)

            self.log.debug(f"[{self.mac_str}] Setting current mode to comfort mode")
            await self.cli.bluetooth_gatt_write(
                address=self.mac_address_int,
                handle=46,
                data=bytes(msg),
                response=True,
                timeout=3)

            open_valve = desired_state

            if open_valve:
                low = (350 % 256) & 255
                high = ((int)(350 / 256)) & 255
                self.log.info(f"[{self.mac_str}] Setting temperature to 35 °C")
            else:
                low = 70 & 255
                high = 0x00
                self.log.info(f"[{self.mac_str}] Setting temperature to 7 °C")

            # set comfort temperature
            msg = bytearray([0xAA, 0xAA, 0x13, 0x0C, 0x00, 0x00, self.next_packet_number(), low, high,
                             low, high, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
            checksum = self.calculate_checksum(msg)
            msg.append(checksum)
            await self.cli.bluetooth_gatt_write(address=self.mac_address_int,
                                                handle=46,
                                                data=bytes(msg),
                                                response=True, timeout=3)

            self.log.info(f"[{self.mac_str}]  Operation Complete")
            return True

        except Exception as e:
            print(e)
            return False

    def set_mode(self, mode):
        self.current_mode = mode

    def set_current_packet_number(self, pkt_number):
        self.current_packet_number = pkt_number

    def set_current_comfort_temperature(self, current_comfort_temp):
        self.current_comfort_temp = current_comfort_temp

    def next_packet_number(self):
        to_ret = self.current_packet_number
        self.current_packet_number = self.current_packet_number + 1
        if self.current_packet_number > 255:
            self.current_packet_number = 1
        return to_ret

    def on_bluetooth_gatt_notify(self, size_array, value):

        self.log.info(f"Received BLE packet: {hexlify(value)}")

        response_length = len(value)

        if self.current_resp == [] and response_length < 2:
            # print("Bad packet response")
            return

        if not self.current_resp:
            if value[0] == 0xAA and value[1] == 0xAA:
                self.current_resp.extend(value)
                val_cut = value[3:]
                if 0x55 in val_cut:
                    return
        else:
            self.current_resp.extend(value)

        response_length = len(self.current_resp)

        received_checksum = self.current_resp[response_length - 1]
        expected_checksum = self.calculate_checksum(self.current_resp[0:response_length - 1])
        self.log.debug("-- Checksums --\n"
                       f"Received: {received_checksum}\n"
                       f"Expected: {expected_checksum}\n"
                       f"-- Pkt. Number --\n"
                       f"Received: {self.current_resp[6]}\n"
                       f"Expected: {self.current_packet_number - 1}\n")

        final = self.current_resp[0:3]

        cut = [i for i in self.current_resp[3:] if i != 0x55]

        final.extend(cut)

        current_resp = final

        # print("CURRENT_RESP")
        # print(current_resp)

        if current_resp[3] != 255 and current_resp[4] != 255 \
                and expected_checksum == received_checksum:

            # se è un pacchetto di risposta a un comando 0x01, 0x00, 0x00 (first group)
            if current_resp[3] == 0x01 and current_resp[4] == 0x00 and current_resp[5] == 0x00:
                self.set_mode(current_resp[response_length - 2])
                self.set_current_packet_number(current_resp[6])
                self.received_response_event.set()

            if not self.got_response_for_current_temp and len(current_resp) >= 9 and current_resp[
                6] == self.current_packet_number - 1 and \
                    current_resp[3] == 0x0C and current_resp[4] == 0x00 and current_resp[5] == 0x00:
                current_comfort_temp = current_resp[8] * 256 + current_resp[7]
                self.set_current_comfort_temperature(current_comfort_temp)
                self.set_current_packet_number(current_resp[6])
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


async def main():
    cli = aioesphomeapi.APIClient("192.168.1.222",
                                  6053,
                                  None)
    await cli.connect(login=True)

    def cb(adv: BluetoothLEAdvertisement):
        #print(f"{adv.address} - {adv.manufacturer_data}")
        pass

    cli.subscribe_bluetooth_le_advertisements(cb)
    await asyncio.sleep(1)

    r = RadiatorValve("62:00:A1:1E:C1:1F", cli)
    await r.set_state(True)
    print("Set state True")
    await asyncio.sleep(10)
    await r.set_state(False)


if __name__ == '__main__':
    asyncio.run(main())
