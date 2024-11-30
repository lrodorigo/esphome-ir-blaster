import threading
from binascii import hexlify
import time
import re

WRITE_CHARACTERISTIC_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"


class RadiatorValve:

    def __init__(self, mac_address: str, cli: aioesphomeapi.APIClient):
        self.cli = cli
        self.mac_address = mac_to_int(mac_address)
        self.current_mode = 0
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

            await self.cli.bluetooth_device_connect(self.mac_address,
                                                    self.on_ble_state,
                                                    timeout=10,
                                                    disconnect_timeout=10,
                                                    address_type=0
                                                    )
            print("Connected to the device")

            await self.cli.bluetooth_gatt_start_notify(self.mac_address,
                                                       handle=48,
                                                       on_bluetooth_gatt_notify=
                                                       lambda size, array: self.on_bluetooth_gatt_notify(size, array))

            self.got_packet_number = False
            self.current_packet_number = 1
            while not self.got_packet_number:
                msg = bytearray([0xAA, 0xAA, 0x07, 0x01, 0x00, 0x00, self.get_packet_number()])
                checksum = calculate_checksum(msg)
                msg.append(checksum)

                await self.cli.bluetooth_gatt_write(address=self.mac_address,
                                                    handle=46,
                                                    data=bytes(msg),
                                                    response=True, timeout=10)

                ret = self.received_response_event.wait(timeout=1)

                if ret:
                    print("Got Current packet number")
                    self.got_packet_number = True

            self.received_response_event.clear()

            print("Get current comfort temp")
            # loop per ottenere la current comfort temp
            self.got_response_for_current_temp = False
            while not self.got_response_for_current_temp:
                msg = bytearray([0xAA, 0xAA, 0x07, 0x0C, 0x00, 0x00, self.get_packet_number()])
                checksum = calculate_checksum(msg)
                msg.append(checksum)
                await self.cli.bluetooth_gatt_write(address=self.mac_address,
                                                    handle=46,
                                                    data=bytes(msg),
                                                    response=True, timeout=3)

                ret = self.received_response_event.wait(timeout=1)
                if ret:
                    self.got_response_for_current_temp = True

            # set comfort mode
            comfort_mode = 0x01
            print("Current mode = " + str(self.current_mode) +
                  " current_packet_number = " + str(self.current_packet_number) +
                  " current comfort temp = " + str(self.current_comfort_temp * 0.1) + "°")

            msg = bytearray([0xAA, 0xAA, 0x13, 0x01, 0x00, 0x00, self.get_packet_number(), comfort_mode, 0x00,
                             0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, self.current_mode])
            checksum = calculate_checksum(msg)
            msg.append(checksum)

            print("Set current mode to comfort mode")
            await self.cli.bluetooth_gatt_write(
                address=self.mac_address,
                handle=46,
                data=bytes(msg),
                response=True,
                timeout=3)

            open_valve = desired_state

            if open_valve:
                low = (350 % 256) & 255
                high = ((int)(350 / 256)) & 255
                print("Set temp to 35")
            else:
                low = 70 & 255
                high = 0x00
                print("Set temp to 7")

            # set comfort temperature
            msg = bytearray([0xAA, 0xAA, 0x13, 0x0C, 0x00, 0x00, self.get_packet_number(), low, high,
                             low, high, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
            checksum = calculate_checksum(msg)
            msg.append(checksum)
            await self.cli.bluetooth_gatt_write(address=self.mac_address,
                                                handle=46,
                                                data=bytes(msg),
                                                response=True, timeout=3)

            done = True
            print("DONEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE")
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

    def get_packet_number(self):
        to_ret = self.current_packet_number
        self.current_packet_number = self.current_packet_number + 1
        if self.current_packet_number > 255:
            self.current_packet_number = 1
        return to_ret

    def on_bluetooth_gatt_notify(self, size_array, value):

        print("Received data: %s" % hexlify(value))

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
        expected_checksum = calculate_checksum(self.current_resp[0:response_length - 1])

        print("RECV CHECKSUM")
        print(received_checksum)
        print("EXPECTED CHECKSUM")
        print(expected_checksum)
        print("RECV_PACKET_NUMBER")
        print(self.current_resp[6])
        print("EXPECTED_PACKET_NUMBER")
        print(self.current_packet_number - 1)

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

            if not self.got_response_for_current_temp and len(current_resp) >= 9 and current_resp[6] == self.current_packet_number - 1 and \
                    current_resp[3] == 0x0C and current_resp[4] == 0x00 and current_resp[5] == 0x00:
                current_comfort_temp = current_resp[8] * 256 + current_resp[7]
                self.set_current_comfort_temperature(current_comfort_temp)
                self.set_current_packet_number(current_resp[6])
                self.received_response_event.set()

        self.current_resp = []





def calculate_checksum(msg):
    msg_to_analyze = msg[3:]
    msg_to_analyze = [i for i in msg_to_analyze if i != 0x55]
    # print(msg_to_analyze)
    checksum = (sum(msg_to_analyze) & 0xFF)
    # print(checksum)
    return checksum



def mac_to_int(mac):
    res = re.match('^((?:(?:[0-9a-f]{2}):){5}[0-9a-f]{2})$', mac.lower())
    if res is None:
        raise ValueError('invalid mac address')
    return int(res.group(0).replace(':', ''), 16)
