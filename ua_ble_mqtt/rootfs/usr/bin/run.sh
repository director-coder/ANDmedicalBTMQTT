#!/usr/bin/env sh
set -eux  # <-- важно: -x печатает команды

echo "=== ua_ble_mqtt run.sh START $(date -Iseconds) ==="
echo "--- options.json ---"
cat /data/options.json || true
echo "--------------------"

CONFIG=/data/options.json

ADDRESS=$(python -c "import json;print(json.load(open('$CONFIG')).get('address',''))")
ADAPTER=$(python -c "import json;print(json.load(open('$CONFIG')).get('adapter','hci0'))")

MQTT_HOST=$(python -c "import json;print(json.load(open('$CONFIG')).get('mqtt_host','core-mosquitto'))")
MQTT_PORT=$(python -c "import json;print(int(json.load(open('$CONFIG')).get('mqtt_port',1883)))")
MQTT_USER=$(python -c "import json;print(json.load(open('$CONFIG')).get('mqtt_username',''))")
MQTT_PASS=$(python -c "import json;print(json.load(open('$CONFIG')).get('mqtt_password',''))")

DISCOVERY_PREFIX=$(python -c "import json;print(json.load(open('$CONFIG')).get('discovery_prefix','homeassistant'))")
BASE_TOPIC=$(python -c "import json;print(json.load(open('$CONFIG')).get('base_topic','anduable'))")
DEVICE_NAME=$(python -c "import json;print(json.load(open('$CONFIG')).get('device_name','AND-UA-BLE'))")

RECONNECT=$(python -c "import json;print(int(json.load(open('$CONFIG')).get('reconnect_seconds',10)))")
PUBLISH_RAW=$(python -c "import json;print(bool(json.load(open('$CONFIG')).get('publish_raw',False)))")

echo "=== parsed: address=$ADDRESS mqtt_host=$MQTT_HOST ==="

exec python -u /usr/src/app/reader.py \
  --address "$ADDRESS" \
  --adapter "$ADAPTER" \
  --mqtt-host "$MQTT_HOST" \
  --mqtt-port "$MQTT_PORT" \
  --mqtt-username "$MQTT_USER" \
  --mqtt-password "$MQTT_PASS" \
  --discovery-prefix "$DISCOVERY_PREFIX" \
  --base-topic "$BASE_TOPIC" \
  --device-name "$DEVICE_NAME" \
  --reconnect-seconds "$RECONNECT" \
  $( [ "$PUBLISH_RAW" = "True" ] && echo "--publish-raw" )
