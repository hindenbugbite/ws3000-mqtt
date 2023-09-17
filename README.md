# Ambient Weather WS-3000 MQTT Client

## Description

This python script collects data from an Ambient Weather WS-3000 base station over USB and publish up to 8 temperatures and humidity over MQTT. Much of the code is a combination from the following sources:

https://github.com/matthewwall/weewx-hp3000
https://github.com/hublol/ws3000-weewx
https://austinsnerdythings.com/2021/03/20/handling-data-from-ambient-weather-ws-2902c-to-mqtt/
https://stevessmarthomeguide.com/adding-an-mqtt-device-to-home-assistant/
https://www.home-assistant.io/integrations/sensor.mqtt/

## Usage

The script is designed to run standalone from both Windows and Linux using Python 3. It requires the modules pyusb and paho-mqtt.

In Linux, the USB access usually needs root access by running python with sudo. But this adds another issue where the modules also needs to be installed with sudo. The solution requires a new udev rules as described here:

https://stackoverflow.com/questions/3738173/why-does-pyusb-libusb-require-root-sudo-permissions-on-linux

The script needs to know at a minimum the MQTT broker address, username, and password to function correctly. This is passed in via command line arguments, or can be hard coded in the script.

    sudo python3 ./ws3000mqtt.py --host mqtt.local --user mqtt_client --password manysecrets

or

    # set MQTT vars
    MQTT_BROKER_HOST  = "core-mosquitto"
    MQTT_BROKER_PORT  = 1883
    MQTT_CLIENT_ID    = "WS3000_Sensor"
    MQTT_USERNAME     = ""
    MQTT_PASSWORD     = ""

The "--test driver" argument is useful to test the USB connection without starting MQTT. It will try to find the WS-3000 base station over USB and print data to screen at the default (or custom) interval.

## MQTT broker troubleshooting

The MQTT client has a prefix of home/ws-3000. This can be changed in the code with the MQTT_TOPIC variable, it "should" apply to the HA discovery messages.

Assuming you run HAOS with add-ons available and a Mosquitto broker add-on, watch/refresh the log to see the MQTT client connect to the broker. The mosquitto add-on shows a hostname "core-mosquitto" and this was not useful since it is inside a docker container. Either specify the IP address or hostname for the HAOS machine. If the the user/password is incorrect, the log will show access denied.

Another debug tool is to use the MQTT-Explorer: https://github.com/thomasnordquist/MQTT-Explorer/releases

This tool allows you to test login to the broker and watch messages update. It also shows the discovery configs that HA receives.

## HA Discovery

This part of the code is designed to make integration with Home Assistant as simple as possible. The script will read the base station config and also a current set of values to determine what the temperature units are set to and also how many sensors are present. This is then used to send out discovery messages for HA to add the same number of sensors.

## Status

Finding/reading WS-3000 over USB works, MQTT client publishing works, Basic HA discovery works. Tested USB on both Windows and Raspbian, MQTT and HA discovery testing on Raspbian.
There is still some mixed python syntax in the code from all the copy and pasting 

## Roadmap

- The number of command line arguments are getting long, so a JSON config file parser might be worth while to save settings.
- This could also allow more customizations where the HA discovery packets can have specific device names defined in the config file
- Eventually this should run as a background task as part of a HA add-on, this needs a docker container which has implications on USB pass through and also USB access like that mentioned above. It might not be possible to make all the system changes from HA.