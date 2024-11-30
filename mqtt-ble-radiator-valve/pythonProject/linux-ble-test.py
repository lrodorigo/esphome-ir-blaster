import pygatt
from binascii import hexlify
import time
import threading
import logging
import sys

# logging.basicConfig()
# logging.getLogger('pygatt').setLevel(logging.DEBUG)


adapter = pygatt.GATTToolBackend(hci_device="hci0")

packet_number = 0x01

current_resp = []

write_characteristic_uuid = "0000ffe9-0000-1000-8000-00805f9b34fb"

received_response_event = threading.Event()
response_mutex = threading.Lock()


class RadiatorValve:

    def __init__(self):
        self.current_mode = 0
        self.current_packet_number = 1
        self.current_comfort_temp = 0

    def set_mode(self, mode):
        self.current_mode = mode

    def set_current_packet_number(self, pkt_number):
        self.current_packet_number = pkt_number

    def set_current_comfort_temperature(self, current_comfort_temp):
        self.current_comfort_temp = current_comfort_temp

    def get_packet_number(self):
        self.current_packet_number = self.current_packet_number + 1
        if self.current_packet_number > 255:
            self.current_packet_number = 1

        return self.current_packet_number


radiator_valve = RadiatorValve()


def calculate_checksum(msg):
    msg_to_analyze = msg[3:]

    msg_to_analyze = [i for i in msg_to_analyze if i != 0x55]

    print(msg_to_analyze)

    checksum = (sum(msg_to_analyze) & 0xFF)

    print(checksum)

    # checksum = (sum(msg[3:]) & 0xFF)

    return checksum


def handle_notification(handle, value):
    global packet_number, event, radiator_valve, response_mutex, current_resp

    """
    handle -- integer, characteristic read handle the data was received on
    value -- bytearray, the data returned in the notification
    """
    print("Received data: %s" % hexlify(value))

    response_length = len(value)

    if current_resp == [] and response_length < 2:
        print("Bad packet response")
        return

    if current_resp == []:
        if value[0] == 0xAA and value[1] == 0xAA:
            current_resp.extend(value)
            val_cut = value[3:]
            if 0x55 in val_cut:
                return
    else:
        current_resp.extend(value)

    response_length = len(current_resp)

    received_checksum = current_resp[response_length - 1]
    expected_checksum = calculate_checksum(current_resp[0:response_length - 1])

    # se non è una bad response
    # print("DECRYPTING")
    # print(packet_number, current_resp[6], expected_checksum, received_checksum)

    final = current_resp[0:3]

    cut = [i for i in current_resp[3:] if i != 0x55]

    final.extend(cut)

    current_resp = final

    print("CURRENT_RESP")
    print(current_resp)

    if current_resp[3] != 255 and current_resp[4] != 255 and current_resp[
        6] == packet_number and expected_checksum == received_checksum:

        # se è un pacchetto di risposta a un comando 0x01, 0x00, 0x00 (first group)
        if current_resp[3] == 0x01 and current_resp[4] == 0x00 and current_resp[5] == 0x00:
            print("Packet num response")
            with response_mutex:
                radiator_valve.set_mode(current_resp[response_length - 2])
                radiator_valve.set_current_packet_number(current_resp[6])

            received_response_event.set()

        if current_resp[3] == 0x0C and current_resp[4] == 0x00 and current_resp[5] == 0x00:
            print("Comfort temp message reponse")
            with response_mutex:
                current_comfort_temp = current_resp[8] * 256 + current_resp[7]
                radiator_valve.set_current_comfort_temperature(current_comfort_temp)
                radiator_valve.set_current_packet_number(current_resp[6])

            received_response_event.set()

    current_resp = []


try:
    print("Started")
    adapter.start()
    if len(sys.argv) < 3:
        print("Usage linux-ble-test.py <valve_mac_address> <0|1>\n"
              "./linux-ble-test.py aa:bb:cc:dd:ee:ff 1")

    device = adapter.connect(sys.argv[1])

    print("Connected to valve")

    device.subscribe("0000ffe4-0000-1000-8000-00805f9b34fb", callback=handle_notification)

    write_characteristic_uuid = "0000ffe9-0000-1000-8000-00805f9b34fb"

    # loop per ottenere packet number e mode corrente
    got_packet_number = False
    while got_packet_number == False:
        msg = bytearray([0xAA, 0xAA, 0x07, 0x01, 0x00, 0x00, packet_number])
        checksum = calculate_checksum(msg)
        msg.append(checksum)
        device.char_write(write_characteristic_uuid, msg, wait_for_response=False)
        print("Char write")
        ret = received_response_event.wait(timeout=1)

        if ret == True:
            print("Current packet number " + str(packet_number))
            got_packet_number = True

        packet_number = packet_number + 1
        if packet_number > 255:
            packet_number = 1

    received_response_event.clear()

    print("Get current comfort temp")
    # loop per ottenere la current comfort temp
    got_response_for_current_temp = False
    while got_response_for_current_temp == False:
        msg = bytearray([0xAA, 0xAA, 0x07, 0x0C, 0x00, 0x00, packet_number])
        checksum = calculate_checksum(msg)
        msg.append(checksum)
        device.char_write(write_characteristic_uuid, msg, wait_for_response=False)
        ret = received_response_event.wait(timeout=1)
        if ret == True:
            got_response_for_current_temp = True

    with response_mutex:
        # set comfort mode
        comfort_mode = 0x01
        print("Current mode = " + str(radiator_valve.current_mode) +
              " current_packet_number = " + str(radiator_valve.current_packet_number) +
              " current comfort temp = " + str(radiator_valve.current_comfort_temp * 0.1) + "°")

        msg = bytearray([0xAA, 0xAA, 0x13, 0x01, 0x00, 0x00, radiator_valve.get_packet_number(), comfort_mode, 0x00,
                         0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, radiator_valve.current_mode])
        checksum = calculate_checksum(msg)
        msg.append(checksum)

        print("Set current mode to comfort mode")
        device.char_write(write_characteristic_uuid, msg, wait_for_response=True)

        open_valve = False

        if int(sys.argv[2]) == 1:
            open_valve = True
        else:
            open_valve = False

        if open_valve:
            low = (350 % 256) & 255
            high = ((int)(350 / 256)) & 255
            print("Set temp to 35")
        else:
            low = 70 & 255
            high = 0x00
            print("Set temp to 7")

        # set comfort temperature
        msg = bytearray([0xAA, 0xAA, 0x13, 0x0C, 0x00, 0x00, radiator_valve.get_packet_number(), low, high,
                         low, high, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        checksum = calculate_checksum(msg)
        msg.append(checksum)
        device.char_write(write_characteristic_uuid, msg, wait_for_response=True)



finally:
    adapter.stop()
