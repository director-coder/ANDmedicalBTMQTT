FROM python:3.12-alpine

RUN apk add --no-cache dbus dbus-libs bluez

# bleak = BLE клиент (BlueZ backend), paho-mqtt = MQTT клиент
RUN pip install --no-cache-dir bleak paho-mqtt

COPY rootfs/ /
RUN chmod +x /usr/bin/run.sh

CMD ["/usr/bin/run.sh"]
