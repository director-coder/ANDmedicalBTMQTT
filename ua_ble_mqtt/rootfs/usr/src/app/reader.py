import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional

import paho.mqtt.client as mqtt
from bleak import BleakClient, BleakScanner


# Standard BLE UUIDs for Blood Pressure Service/Measurement
BLOOD_PRESSURE_SERVICE = "00001810-0000-1000-8000-00805f9b34fb"
BP_MEASUREMENT_CHAR = "00002a35-0000-1000-8000-00805f9b34fb"


def mac_to_id(mac: str) -> str:
    return mac.lower().replace(":", "")


def sfloat_to_float(raw: int) -> float:
    """
    IEEE-11073 SFLOAT (16-bit): 12-bit mantissa, 4-bit exponent (base 10).
    """
    mantissa = raw & 0x0FFF
    exponent = (raw >> 12) & 0x000F

    # sign extend mantissa 12-bit
    if mantissa >= 0x0800:
        mantissa = mantissa - 0x1000

    # sign extend exponent 4-bit
    if exponent >= 0x08:
        exponent = exponent - 0x10

    return float(mantissa) * (10.0 ** float(exponent))


@dataclass
class BPReading:
    systolic: float
    diastolic: float
    map: float
    pulse: Optional[float] = None
    timestamp_iso: Optional[str] = None
    unit: str = "mmHg"


def parse_bp_measurement(data: bytes) -> BPReading:
    """
    Parse Blood Pressure Measurement characteristic (0x2A35).
    Format per Bluetooth Blood Pressure Service spec.
    """
    if len(data) < 1 + 6:
        raise ValueError("Packet too short for BP measurement")

    flags = data[0]
    unit_kpa = bool(flags & 0x01)  # 0=mmHg, 1=kPa

    # Compound value: 3 x SFLOAT (systolic, diastolic, MAP)
    sys_raw = int.from_bytes(data[1:3], "little")
    dia_raw = int.from_bytes(data[3:5], "little")
    map_raw = int.from_bytes(data[5:7], "little")

    systolic = sfloat_to_float(sys_raw)
    diastolic = sfloat_to_float(dia_raw)
    map_v = sfloat_to_float(map_raw)

    idx = 7
    ts_iso = None
    pulse = None

    # Timestamp present
    if flags & 0x02:
        if len(data) < idx + 7:
            raise ValueError("Packet too short for timestamp")
        year = int.from_bytes(data[idx:idx+2], "little"); idx += 2
        month = data[idx]; idx += 1
        day = data[idx]; idx += 1
        hour = data[idx]; idx += 1
        minute = data[idx]; idx += 1
        second = data[idx]; idx += 1
        ts_iso = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"

    # Pulse Rate present
    if flags & 0x04:
        if len(data) < idx + 2:
            raise ValueError("Packet too short for pulse")
        pulse_raw = int.from_bytes(data[idx:idx+2], "little")
        pulse = sfloat_to_float(pulse_raw)
        idx += 2

    # If unit is kPa, convert to mmHg for HA-friendly display
    unit = "kPa" if unit_kpa else "mmHg"
    if unit_kpa:
        kpa_to_mmhg = 7.50062
        systolic *= kpa_to_mmhg
        diastolic *= kpa_to_mmhg
        map_v *= kpa_to_mmhg
        unit = "mmHg"

    return BPReading(systolic=systolic, diastolic=diastolic, map=map_v, pulse=pulse, timestamp_iso=ts_iso, unit=unit)


class MqttPublisher:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.client = mqtt.Client()
        if username:
            self.client.username_pw_set(username, password or None)
        self.host = host
        self.port = port
        self._connected = asyncio.Event()

        def on_connect(client, userdata, flags, rc):
            print(f"[mqtt] connected rc={rc}")
            self._connected.set()

        def on_disconnect(client, userdata, rc):
            print(f"[mqtt] disconnected rc={rc}")
            self._connected.clear()

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect

    def connect(self):
        print(f"[mqtt] connecting to {self.host}:{self.port} ...")
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()

    async def wait_connected(self, timeout: int = 10):
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("MQTT connect timeout (check mqtt_host/mqtt_port)")

    def publish_json(self, topic: str, payload: dict, retain: bool = True):
        self.client.publish(topic, json.dumps(payload), retain=retain)

    def publish_str(self, topic: str, payload: str, retain: bool = True):
        self.client.publish(topic, payload, retain=retain)


def publish_discovery(mq: MqttPublisher, discovery_prefix: str, device_id: str, device_name: str, base_topic: str, mac: str):
    """
    Create 3 MQTT-discovery sensors: systolic, diastolic, pulse.
    Uses a single JSON state topic.
    """
    state_topic = f"{base_topic}/{device_id}/state"
    availability_topic = f"{base_topic}/{device_id}/availability"

    device_block = {
        "identifiers": [device_id],
        "name": device_name,
        "manufacturer": "A&D",
        "model": "UA-651BLE",
        "connections": [["mac", mac]],
    }

    sensors = [
        ("systolic", "Systolic", "mmHg", "pressure", "{{ value_json.systolic }}"),
        ("diastolic", "Diastolic", "mmHg", "pressure", "{{ value_json.diastolic }}"),
        ("pulse", "Pulse", "bpm", "frequency", "{{ value_json.pulse }}"),
    ]

    for key, name, unit, device_class, template in sensors:
        topic = f"{discovery_prefix}/sensor/{device_id}/{key}/config"
        payload = {
            "name": f"{device_name} {name}",
            "unique_id": f"{device_id}_{key}",
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "unit_of_measurement": unit,
            "device_class": device_class,
            "state_class": "measurement",
            "value_template": template,
            "device": device_block,
        }
        mq.publish_json(topic, payload, retain=True)

    # Mark online
    mq.publish_str(availability_topic, "online", retain=True)


async def run_reader(args):
    device_id = mac_to_id(args.address)
    mq = MqttPublisher(args.mqtt_host, args.mqtt_port, args.mqtt_username, args.mqtt_password)
    mq.connect()
    print(f"[start] address={args.address} mqtt={args.mqtt_host}:{args.mqtt_port} base_topic={args.base_topic} publish_raw={args.publish_raw}")
    await mq.wait_connected(10)

    publish_discovery(
        mq= mq,
        discovery_prefix=args.discovery_prefix,
        device_id=device_id,
        device_name=args.device_name,
        base_topic=args.base_topic,
        mac=args.address,
    )

    state_topic = f"{args.base_topic}/{device_id}/state"
    raw_topic = f"{args.base_topic}/{device_id}/raw"

    while True:
        try:
            print(f"[ble] Connecting to {args.address} ...")
            async with BleakClient(args.address) as client:
                print("[ble] Connected. Subscribing to BP measurement notifications...")
                services = await client.get_services()
                print(f"[ble] services={len(services)}")
                for s in services:
                    if "1810" in s.uuid:  # blood pressure service
                        print(f"[ble] service {s.uuid}")
                        for ch in s.characteristics:
                            print(f"[ble]  char {ch.uuid} props={ch.properties}")

                def on_notify(_char, data: bytearray):
                    b = bytes(data)
                    try:
                        reading = parse_bp_measurement(b)
                        payload = {
                            "systolic": round(reading.systolic, 1),
                            "diastolic": round(reading.diastolic, 1),
                            "map": round(reading.map, 1),
                            "pulse": (round(reading.pulse, 1) if reading.pulse is not None else None),
                            "timestamp": reading.timestamp_iso or time.strftime("%Y-%m-%dT%H:%M:%S"),
                        }
                        print(f"[bp] {payload}")
                        mq.publish_json(state_topic, payload, retain=True)

                        if args.publish_raw:
                            mq.publish_str(raw_topic, b.hex(), retain=False)
                    except Exception as e:
                        print(f"[bp] parse/publish error: {e}")
                        if args.publish_raw:
                            mq.publish_str(raw_topic, b.hex(), retain=False)

                await client.start_notify(BP_MEASUREMENT_CHAR, on_notify)

                # Keep running; BLE device usually sends only when measurement completes
                while True:
                    await asyncio.sleep(5)

        except Exception as e:
            print(f"[ble] Error: {e}. Reconnecting in {args.reconnect_seconds}s")
            # mark offline
            mq.publish_str(f"{args.base_topic}/{device_id}/availability", "offline", retain=True)
            await asyncio.sleep(args.reconnect_seconds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True)
    ap.add_argument("--adapter", default="hci0")  # reserved for future
    ap.add_argument("--mqtt-host", required=True)
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-username", default="")
    ap.add_argument("--mqtt-password", default="")
    ap.add_argument("--discovery-prefix", default="homeassistant")
    ap.add_argument("--base-topic", default="ua651ble")
    ap.add_argument("--device-name", default="UA-651BLE")
    ap.add_argument("--reconnect-seconds", type=int, default=10)
    ap.add_argument("--publish-raw", action="store_true")
    args = ap.parse_args()

    asyncio.run(run_reader(args))


if __name__ == "__main__":
    main()
