#!/usr/bin/env python
#
"""Classes and functions for interfacing with an Ambient Weather WS-3000
station.

The following references were useful for developing this driver. More than simply useful,
in fact, since a lot of material has been directly reused:
    
From Matthew Wall:
  https://github.com/matthewwall/weewx-hp3000

From Hublol, the USB driver for connecting WS-3000 to weewx
  https://github.com/hublol/ws3000-weewx

Example for MQTT client in python
  https://austinsnerdythings.com/2021/03/20/handling-data-from-ambient-weather-ws-2902c-to-mqtt/

Example for HA discovery protocol
  https://stevessmarthomeguide.com/adding-an-mqtt-device-to-home-assistant/

The notes below are directly from hublol: 

NOTE for Raspberry Pi: if the usb read/write timeout is too small (100ms), errors
might occur when sending/fetching data from the console. It has been increased to 1000 by default,
but if this is still not sufficient futher increase the timeout in the weewx configuration file.

The comments below are taken directly from Matthew Wall's driver. They are included here for reference:

The HP-3000 supports up to 8 remote temperature/humidity sensors.  The console
has a 3"x4" TFT color display, with graph and room for 5 sensor displays.  The
sensor display regions can cycle through different sensors when more than 5
sensors are connected.
Every configuration option in the console can be modified via software.  These
options includes:
 - archive interval
 - date/time
 - date/time format
 - timezone
 - which sensors should be displayed in the 5 display regions
 - horizontal graph axis scaling
 - vertical graph axis
 - calibration for temperature/humidity from each sensor
 - alarms
 - historical data
Historical data are saved to the (optional) microSD card.  If no card is
installed, no data are retained.
Each sensor has its own display of temperature and humidity.
Each sensor is identified by channel number.  The channel number is set using
DIP switches on each sensor.  The DIP switches also determine which units will
be displayed in each sensor.
There are 4 two-position DIP switches.  DIP 4 determines units: 0=F 1=C
DIP 1-3 determine which of 8 channels is selected.
Each sensor uses 2 AA batteries.  Nominal battery life is 1 year.
The console uses 5V DC from an AC/DC transformer.
Data from sensors are received every 60 seconds.
Dewpoint and heatindex are calculated within the console.
Temperature sensors measure to +/- 2 degree F
Humidity sensors measure to +/- 5 %
Calibrations are applied in the console, so the values received from the
console are calibrated.  Calculations in the console are performed in degree C.
The console has a radio controlled clock.  During RCC reception, no data will
be transmitted.  If no RCC is received, attempt will be made every two hours
until successful.
This driver was developed without any assistance from Ambient Weather (the
vendor) or Fine Offset (the manufacturer).
===============================================================================
Messages from console
The console sends data in 64-byte chunks.  It looks like the console reuses a
buffer, because each message shorter than the previous contains bytes from the
previous message.  The byte sequence 0x40 0x7d indicates end of data within a
buffer.
Many of the console messages correspond with the control messages sent from
the host.

<...>

current data (27 bytes)
00 7b
01 00 ch1 temp MSB
02 eb ch1 temp LSB    t1 = (signed short(MSB,LSB)) / 10.0 - NB: modified to handle negative values
03 25 ch1 hum         h1 = hum
04 7f ch2 temp MSB
05 ff ch2 temp LSB
06 ff ch2 hum
07 7f ch3 temp MSB
08 ff ch3 temp LSB
09 ff ch3 hum
0a 7f ch4 temp MSB
0b ff ch4 temp LSB
0c ff ch4 hum
0d 7f ch5 temp MSB
0e ff ch5 temp LSB
0f ff ch5 hum
10 7f ch6 temp MSB
11 ff ch6 temp LSB
12 ff ch6 hum
13 7f ch7 temp MSB
14 ff ch7 temp LSB
15 ff ch7 hum
16 7f ch8 temp MSB
17 ff ch8 temp LSB
18 ff ch8 hum
19 40
1a 7d

Change log:

v0.1 - Initial release, syntax updated to Python 3
v0.2 - changed the availability to match will, single availability for all sensors
<...>

"""

import time
import logging
import usb.core
import usb.util
import sys
import traceback
import struct
import json
import usb
import paho.mqtt.client as mqtt
import datetime
import threading

DRIVER_VERSION = "0.2"

# set MQTT vars
MQTT_BROKER_HOST  = "core-mosquitto"
MQTT_BROKER_PORT  = 1883
MQTT_CLIENT_ID    = "WS3000_Sensor"
MQTT_USERNAME     = ""
MQTT_PASSWORD     = ""

# looking to get resultant topic like weather/ws-2902c/[item]
MQTT_TOPIC_PREFIX = "ws-3000/misol"
MQTT_TOPIC           = MQTT_TOPIC_PREFIX + "/sensors"

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
log = logging.getLogger(__name__)


def logmsg(level, msg):
    # syslog.syslog(level, 'ws3000: %s' % msg)
    log.debug(msg)

def logdbg(msg):
    # logmsg(syslog.LOG_DEBUG, msg)
    log.debug(msg)

def loginf(msg):
    # logmsg(syslog.LOG_INFO, msg)
    log.info(msg)

def logerr(msg):
    # logmsg(syslog.LOG_ERR, msg)
    log.error(msg)

def tohex(buf):
    """Helper function used to print a byte array in hex format"""
    if buf:
        return "%s (len=%s)" % (' '.join(["%02x" % x for x in buf]), len(buf))
    return ''

# mostly copied + pasted from https://www.emqx.io/blog/how-to-use-mqtt-in-python and some of my own MQTT scripts
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        loginf(f"connected to MQTT broker at {MQTT_BROKER_HOST}")
        client.publish(MQTT_TOPIC_PREFIX + "/status", 'Online', qos=2, retain=True)

    else:
        logerr("Failed to connect, return code %d\n", rc)

def on_disconnect(client, userdata, rc):
    loginf("disconnected from MQTT broker")

def publish(client, topic, msg):
    result = client.publish(topic, msg)
    # result: [0, 1]
    status = result[0]

    # uncomment for debug. don't need all the success messages.
    if status == 0:
        #print(f"Sent {msg} to topic {topic}")
        pass
    else:
        print(f"Failed to send message to topic {topic}")

class WS3000():
    """Driver for the WS3000 station."""

    COMMANDS = {
        'sensor_values': 0x03,
        'calibration_values': 0x05,
        'interval_value': 0x41,
        'unknown': 0x06,
        'temp_alarm_configuration': 0x08,
        'humidity_alarm_configuration': 0x09,
        'device_configuration': 0x04,
        'synctime': 0x30,
    }

    def __init__(self, **stn_dict):
        """Initialize an object of type WS3000.
        
        NAMED ARGUMENTS:
        
        model: Which station model is this?
        [Optional. Default is 'WS3000']

        timeout: How long to wait, in seconds, before giving up on a response
        from the USB port.
        [Optional. Default is 1000 milliseconds]
        
        wait_before_retry: How long to wait before retrying.
        [Optional. Default is 5 seconds]

        max_tries: How many times to try before giving up.
        [Optional. Default is 3]
        
        vendor_id: The USB vendor ID for the WS3000
        [Optional. Default is 0x0483]
        
        product_id: The USB product ID for the WS3000
        [Optional. Default is 0xca01]
        
        interface: The USB interface
        [Optional. Default is 0]
        
        loop_interval: The time (in seconds) between emitting LOOP packets.
        [Optional. Default is 10]
        
        packet_size: The size of the data fetched from the WS3000 during each read.
        [Optional. Default is 64 (0x40)]
        
        mode: Can be 'Windows', 'MacOS' or 'Linux'. Adjusts some USB code based on operating system
        [Optional. Default is Linux]
        
        """

        # The following variables will in fact be fetched from the device itself.
        # There are anyway declared here with the usual values for the WS3000.
        self.IN_ep = 0x82
        self.OUT_ep = 0x1

        loginf('driver version is %s' % DRIVER_VERSION)
        self.model = stn_dict.get('model', 'WS3000')
        self.record_generation = stn_dict.get('record_generation', 'software')
        self.timeout = int(stn_dict.get('timeout', 1000))
        self.wait_before_retry = float(stn_dict.get('wait_before_retry', 5.0))
        self.max_tries = int(stn_dict.get('max_tries', 3))
        self.loop_interval = int(stn_dict.get('loop_interval', 10))
        self.vendor_id = int(stn_dict.get('vendor_id', '0x0483'), 0)
        self.product_id = int(stn_dict.get('product_id', '0x5750'), 0)
        self.interface = int(stn_dict.get('interface', 0))
        self.packet_size = int(stn_dict.get('packet_size', 64))  # 0x40
        self.mode = stn_dict.get('mode', 'Linux')

        self.device = None
        self.units = 'Celsius'
        self.open_port()

        self.lock = threading.Lock()

    def open_port(self):
        """Establish a connection to the WS3000"""

        loginf("Starting initialization of the WS-3000 driver")

        # try to find the device using the vend and product id
        self.device = self._find_device()

        # TODO: review this piece of code...
        # this is very poorly coded: at first the interface is an 'int', hardcoded to 0, but
        # it is then later assigned the result of usb.util.find_descriptor()... Beside,
        # this requires a re-initialization back to an 'int' if a commucation retry occurs.
        self.interface = 0
        if not self.device:
            logerr("Unable to find USB device (0x%04x, 0x%04x)" %
                   (self.vendor_id, self.product_id))
        for line in str(self.device).splitlines():
            logdbg(line)

        # reset device, required if it was previously left in a 'bad' state
        self.device.reset()

        # Detach any interfaces claimed by the kernel only if not in Windows
        if self.mode != 'Windows':
            if self.device.is_kernel_driver_active(self.interface):
                print("Detaching kernel driver")
                self.device.detach_kernel_driver(self.interface)

        # get the interface and IN and OUT end points
        self.device.set_configuration()
        configuration = self.device.get_active_configuration()
        self.interface = usb.util.find_descriptor(
             configuration, bInterfaceNumber=self.interface
        ) # following this call, the interface is no longer an int...
        self.OUT_ep = usb.util.find_descriptor(
            self.interface,
            # match the first OUT endpoint
            custom_match=lambda eo: \
            usb.util.endpoint_direction(eo.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.IN_ep = usb.util.find_descriptor(
            self.interface,
            # match the first OUT endpoint
            custom_match=lambda ei: \
            usb.util.endpoint_direction(ei.bEndpointAddress) == \
            usb.util.ENDPOINT_IN)

        # The following is normally not required... could be removed?
        try:
            usb.util.claim_interface(self.device, self.interface)
        except usb.USBError as e:
            self.closePort()
            logerr("Unable to claim USB interface: %s" % e)

        loginf("WS-3000 initialization complete")

    def closePort(self):
        """Tries to ensure that the device will be properly 'unclaimed' by the driver"""

        if self.mode == 'simulation':
            return

        try:
            usb.util.dispose_resources(self.device)
        except usb.USBError:
            try:
                self.device.reset()
            except usb.USBError:
                pass

    """
    Function that only returns the current sensors data.
    Should be used by a data service that will add temperature data to an existing packet, for
    example, since a single measurement would be required in such a case.
    """
    def get_current_values(self):
        with self.lock:
            nberrors = 0
            while nberrors < self.max_tries:
                # Get a stream of raw packets, then convert them
                try:
                    read_sensors_command = self.COMMANDS['sensor_values']
                    raw_data = self._get_raw_data(read_sensors_command)
                    #
                    if not raw_data:  # empty record
                        raise Exception("Failed to get any data from the station")
                    formatted_data = self._raw_to_data(raw_data, read_sensors_command)
                    logdbg('data: %s' % formatted_data)
                    #new_packet = self._data_to_wxpacket(formatted_data)
                    #logdbg('packet: %s' % new_packet)
                    #return new_packet
                    return formatted_data
                except (usb.USBError, Exception) as e:
                    exc_traceback = traceback.format_exc()
                    logerr("WS-3000: An error occurred while generating loop packets")
                    logerr(exc_traceback)
                    nberrors += 1
                    # The driver seem to 'loose' connectivity with the station from time to time.
                    # Trying to close/reopen the USB port to fix the problem.
                    self.closePort()
                    self.open_port()
                    time.sleep(self.wait_before_retry)
            logerr("Max retries exceeded while fetching USB reports")
            traceback.print_exc(file=sys.stdout)

    def genLoopPackets(self):
        """Generator function that continuously returns loop packets"""
        try:
            while True:
                lstart = time.time()
                loop_packet = self.get_current_values()
 
                yield loop_packet
                lduration = time.time() - lstart
 
                time.sleep(self.loop_interval - lduration)

        except GeneratorExit:
            pass

    """
    Send command to read the station configuration
    Two pieces of information are saved: the units the station is set to,
    and try to determine the number of sensors attached which will go on
    to help set the number of HA discovery messages to send
    """
    def getDeviceConfig(self):
        with self.lock:
            try:
                # Read the station config
                command = self.COMMANDS["device_configuration"]
                raw = self._get_raw_data(command)
                data = self._raw_to_data(raw, command)
                # Save the units for later use
                if data['type'] == 'device_configuration':
                    if data['units'] == 'F':
                        self.units = 'Fahrenheit'
                    else:
                        self.units = 'Celsius'
                return data
            except (usb.USBError, Exception) as e:
                exc_traceback = traceback.format_exc()
                logerr("WS-3000: An error occurred while generating loop packets")
                logerr(exc_traceback)

    def synTime(self):
        # sync time command (0x30)
        # 00 7b
        # 01 30
        # 02 07 year MSB
        # 03 e0 year LSB
        # 04 0c month
        # 05 11 day-of-month
        # 06 00 hour
        # 07 27 minute
        # 08 0c second
        # 09 06 timezone
        # 0a 40
        # 0b 7d
        with self.lock:
            lnow = datetime.datetime.now()
            ltimezone = int(lnow.astimezone().utcoffset().total_seconds() / 3600)
            lnowtuple = lnow.timetuple()
            lpackedtime = list(struct.pack('>hBBBBBB', lnowtuple[0], lnowtuple[1], lnowtuple[2], lnowtuple[3], lnowtuple[4], lnowtuple[5], ltimezone))
            sequence = [0x7b, self.COMMANDS['synctime']] + lpackedtime + [0x40, 0x7d]

            try:
                logdbg("sending request for 'synctime'")
                self._write_usb(sequence)

            except Exception:
                exc_traceback = traceback.format_exc()
                logerr("WS-3000: An error occurred while fetching data")
                logerr(exc_traceback)
                traceback.print_exc(file=sys.stdout)

    @property
    def hardware_name(self):
        return self.model

    # ===============================================================================
    #                         USB functions
    # ===============================================================================

    def _find_device(self):
        """Find the given vendor and product IDs on the USB bus"""
        device = usb.core.find(idVendor=self.vendor_id, idProduct=self.product_id)
        return device

    def _write_usb(self, buf):
        logdbg("write: %s - timeout: %d" % (tohex(buf), self.timeout))
        if self.mode == 'Windows':
            buf = buf + (64-len(buf))*[0]
        # NB: timeout increased from 100 to 1000 to avoid failure on RPi
        return self.device.write(self.OUT_ep, data=buf, timeout=self.timeout)

    def _read_usb(self):
        logdbg("reading " + str(self.packet_size) + " bytes")
        buf = self.device.read(self.IN_ep, self.packet_size, timeout=self.timeout)
        if not buf:
            return None
        logdbg("read: %s" % tohex(buf))
        if len(buf) != 64:
            logdbg('read: bad buffer length: %s != 64' % len(buf))
            return None
        if buf[0] != 0x7b:
            logdbg('read: bad first byte: 0x%02x != 0x7b' % buf[0])
            return None
        idx = None
        for i in range(0, len(buf) - 1):
            if buf[i] == 0x40 and buf[i + 1] == 0x7d:
                idx = i
                break
        if idx is None:
            logdbg('read: no terminating bytes in buffer: %s' % tohex(buf))
            return None

        return buf[1: idx]

    # =========================================================================
    # LOOP packet related functions
    # ==========================================================================

    def _get_cmd_name(self, hex_command):
        return list(self.COMMANDS.keys())[list(self.COMMANDS.values()).index(hex_command)]

    def _get_raw_data(self, hex_command=COMMANDS['sensor_values']):
        """Get a sequence of bytes from the console."""
        sequence = [0x7b, hex_command, 0x40, 0x7d]
        try:
            logdbg("sending request for " + self._get_cmd_name(hex_command))
            self._write_usb(sequence)
            logdbg("reading results...")
            buf = self._read_usb()
            return buf
        except Exception:
            exc_traceback = traceback.format_exc()
            logerr("WS-3000: An error occurred while fetching data")
            logerr(exc_traceback)
            traceback.print_exc(file=sys.stdout)

    def _raw_to_data(self, buf, hex_command=COMMANDS['sensor_values']):
        """Convert the raw bytes sent by the console to human readable values."""
        logdbg("extracting values for " + self._get_cmd_name(hex_command))
        logdbg("raw: %s" % buf)
        record = dict()
        if not buf:
            return record
        if hex_command == self.COMMANDS['sensor_values']:
            if len(buf) != 24:
                raise Exception("Incorrect buffer length, failed to read " + self._get_cmd_name(hex_command))

            record['units'] = self.units

            for ch, idx in enumerate(range(0, len(buf), 3), 1):
                if list(buf[idx: idx + 3]) == [0x7f, 0xff, 0xff]:
                    continue

                sensordata = struct.unpack(">hB", buf[idx: idx + 3])

                record[f'temperature_CH{ch}'] = sensordata[0] / 10.0
                if (sensordata[1]) <= 100:
                    record[f'humidity_CH{ch}'] = sensordata[1]

                if self.units == 'Fahrenheit':
                    record[f'temperature_CH{ch}'] = record[f'temperature_CH{ch}'] * 1.8 + 32

        elif hex_command == self.COMMANDS['device_configuration']:
            if len(buf) != 27:
                raise Exception("Incorrect buffer length, failed to read " + self._get_cmd_name(hex_command))
            record['type'] = self._get_cmd_name(hex_command)
            if buf[7] == 1:
                record['units'] = 'F'
            else:
                record['units'] = 'C'
        else:
            logdbg("unknown data: %s" % tohex(buf))
        return record

def publish_results(result):
    """ result is a dict. full list of variables include:
    Temperature_CH[1-8]: temp_data, Humidity_CH[1-8]: hum_data"""

    # we're just going to publish everything. less coding.
    for key in result:
        #print(f"{key}: {result[key]}")
        # resultant topic is home/ws-3000/temperature_CHn or humidity_CHn
        specific_topic = MQTT_TOPIC + f"/{key}"
        msg = str(result[key])
        logdbg(f"attempting to publish to {specific_topic} with message {msg}")
        publish(client, specific_topic, msg)

def publish_HAdiscovery(_c, data):
    # Publishing a set of auto discovery messages for Home Assistant
    # base on information from: https://stevessmarthomeguide.com/adding-an-mqtt-device-to-home-assistant/
    # total number of sensors are passed in from ws-3000 getDeviceConfig function along with temperature units

    # Read one set of current values
    command = _c.COMMANDS["sensor_values"]
    raw = _c._get_raw_data(command)
    data2 = _c._raw_to_data(raw, command)
    # Try to determine the number of sensors available
    totalSensors = int((len(data2) - 1)/2)

    # we're just going to publish a HA discovery message for each available sensor.
    # The state_topic needs to be the same as what is published
    # Eventually the name can be customized from a config file
    # Be careful when running multiple stations, HA will throw exception if unique_ID is duplicated
    # Could add a user provided station ID if multiple ws-3000 is used
    for ch in range(1,(totalSensors+1)):
        payloadT = {
            "unique_id": f"ws3000_temp_ch{ch}",
            "name": f"Temperature Sensor {ch}",
            "state_topic": MQTT_TOPIC + f"/temperature_CH{ch}",
            "availability_topic": MQTT_TOPIC_PREFIX + "/status",
            "expire_after":"3600",
            "suggested_display_precision": 1,
            "unit_of_measurement": f"°{data['units']}"
        }
        payloadH = {
            "unique_id": f"ws3000_hum_ch{ch}",
            "name": f"Humidity Sensor {ch}",
            "state_topic": MQTT_TOPIC + f"/humidity_CH{ch}",
            "availability_topic": MQTT_TOPIC_PREFIX + "/status",
            "expire_after":"3600",
            "unit_of_measurement": "%",
            "icon": "mdi:water-percent"
        }
        specific_topic = f"homeassistant/sensor/temp_ch{ch}/config"
        msg = json.dumps(payloadT) #convert to JSON
        logdbg(f"attempting to publish HA discovery for temperature sensor {ch}")
        #publish(client, specific_topic, msg, 0, True)
        client.publish(specific_topic, msg, qos=0, retain=True)
        specific_topic = f"homeassistant/sensor/hum_ch{ch}/config"
        msg = json.dumps(payloadH) #convert to JSON
        logdbg(f"attempting to publish HA discovery for humidity sensor {ch}")
        #publish(client, specific_topic, msg, 0, True)
        client.publish(specific_topic, msg, qos=0, retain=True)

def syncTimeFn(_station):
    while True:
        lcurtime = datetime.datetime.now()
        lnextwakeup = lcurtime + datetime.timedelta(hours=12)

        try:
            _station.syncTime()

        except:
            pass

        time.sleep((lnextwakeup - datetime.datetime.now()).seconds)

# *******************************************************************
#
# define a main entry point for basic testing of the station.
#
if __name__ == '__main__':

    import argparse, platform
    os = platform.system()

    parser = argparse.ArgumentParser(description='Poll a WS-3000 base station over USB and publish up to 8 temperatures and humidity over MQTT')
    parser.add_argument('--version', action='store_true',
                        help='display driver version')
    parser.add_argument('--debug', action='store_true',
                        help='display diagnostic information while running')
    parser.add_argument('--test', default='station', choices=['station', 'driver'],
                        help='what to test: station or driver')
    parser.add_argument('--interval', default=60,
                        help='set polling rate in seconds')
    parser.add_argument('--host', default=None,
                        help='specify MQTT broker name')
    parser.add_argument('--port', default=None,
                        help='specify MQTT broker port')
    parser.add_argument('--user', default=None,
                        help='specify MQTT broker username, a password must also be used')
    parser.add_argument('--password', default=None,
                        help='specify MQTT broker password, cannot be empty')
    parser.add_argument('--publish', default="hadiscovery", choices=['hadiscovery', 'lwt'],
                        help='specify MQTT broker port')    
    parser.add_argument('--synctime', action='store_true', 
                        help='sync time with methestation')    
    options = parser.parse_args()

    if options.version:
        print(f"driver version {DRIVER_VERSION}")
        exit(1)
    
    poll_interval = options.interval

    if options.host:
        MQTT_BROKER_HOST = options.host

    if options.port:
        MQTT_BROKER_PORT = options.port

    if options.debug:
        log.setLevel(logging.DEBUG)

    station = WS3000(loop_interval=poll_interval,mode=os)

    if options.synctime:
        synctimethread = threading.Thread(target=syncTimeFn, args = (station, ), daemon=True)
        synctimethread.start()

    # Driver mode only reads from USB and print to screen
    if options.test == 'driver':
        try:
            # Grab station configuration
            data = station.getDeviceConfig()
            print('data: %s' % data)
            # This runs forever with loop_interval delay
            for p in station.genLoopPackets():
                print(p)
        finally:
            station.closePort()
    # Any other mode will start MQTT client
    else:
        # set up mqtt client
        client = mqtt.Client(client_id=MQTT_CLIENT_ID)
        if MQTT_USERNAME and MQTT_PASSWORD:
            client.username_pw_set(MQTT_USERNAME,MQTT_PASSWORD)
            print("Username and password set.")
        client.will_set(MQTT_TOPIC_PREFIX+"/status", payload="Offline", qos=2, retain=True) # set LWT     
        client.on_connect = on_connect # on connect callback
        client.on_disconnect = on_disconnect # on disconnect callback

        if options.user and options.password:
            client.username_pw_set(options.user,options.password)

        try:
            # connect to MQTT broker
            client.connect(MQTT_BROKER_HOST, port=MQTT_BROKER_PORT)
            client.loop_start()
            # Grab station configuration
            data = station.getDeviceConfig()
            # Send out HA MQTT discovery

            if options.publish == "hadiscovery":
                publish_HAdiscovery(client, data)

            # This runs forever with loop_interval delay
            for p in station.genLoopPackets():
                publish_results(p)

        finally:
            station.closePort()
