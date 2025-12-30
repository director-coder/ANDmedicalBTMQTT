import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import paho.mqtt.client as mqtt
from bleak import BleakClient, BleakScanner

# BLE UUID: Blood Pressure Measurement (0x2A35)
BP_MEASUREMENT_CHAR = "00002a35-0000-1000-8000-00805f9b34fb"

DEFAULT_IDLE_WATCHDOG = 45  # seconds without notifications before reconnect
DEFAULT_SCAN_TIMEOUT = 30   # seconds


def normalize_mac(addr: str) -> str:
    """Accept 6C:EC:... or 6CEC... or 6c-ec-... and return 6C:EC:"""
    a = addr.strip().upper().replace("-", "").replace(":", "")
    if len(a) == 12:
        return ":".join(a[i:i + 2] for i in range(0, 12, 2))
    return addr.strip()


def mac_to_id(mac: str) -> str:
    return mac.lower().replace(":", "")


def sfloat_to_float(raw: int) -> Optional[float]:
    """
    IEEE-11073 SFLOAT (16-bit). Return None for special/invalid values.
    Common special values:
      0x07FF = NaN
      0x07FE = +INF
      0x0802 = -INF
      0x0800 = NRes
    """
    if raw in (0x07FF, 0x07FE, 0x0802, 0x0800):
        return None

    mantissa = raw & 0x0FFF
    exponent = (raw >> 12) & 0x000F

    # sign extend mantissa 12-bit
    if mantissa >= 0x0800:
        mantissa -= 0x1000

    # sign extend exponent 4-bit
    if exponent >= 0x08:
        exponent -= 0x10

    return float(mantissa) * (10.0 ** float(exponent))


@dataclass
class BPReading:
    systolic: float
    diastolic: float
    map: float
    pulse: Optional[float]
    timestamp_iso: Optional[str]


def parse_bp_measurement(data: bytes) -> BPReading:
    """
    Parse Blood Pressure Measurement characteristic (0x2A35).
    Filters intermediate frames by rejecting SFLOAT special values (NaN/NRes/INF).
    """
    if len(data) < 7:
        raise ValueError("Packet too short")

    flags = data[0]
    unit_kpa = bool(flags & 0x01)  # 0=mmHg, 1=kPa

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
        year = int.from_bytes(data[idx:idx + 2], "little"); idx += 2
        month = data[idx]; idx += 1
        day = data[idx]; idx += 1
        hour = data[idx]; idx += 1
        minute = data[idx]; idx += 1
        second = data[idx]; idx += 1
        ts_iso = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"

    # Pulse present
    if flags & 0x04:
        if len(data) < idx + 2:
            raise ValueError("Packet too short for pulse")
        pulse_raw = int.from_bytes(data[idx:idx + 2], "little")
        pulse = sfloat_to_float(pulse_raw)

    # Reject intermediate/invalid frames
    if systolic is None or diastolic is None or map_v is None:
        raise ValueError("Intermediate/invalid BP frame (sfloat special)")
    if (flags & 0x04) and pulse is None:
        raise ValueError("Intermediate/invalid pulse frame (sfloat special)")

    # Convert kPa -> mmHg if needed
    if unit_kpa:
        kpa_to_mmhg = 7.50062
        systolic *= kpa_to_mmhg
        diastolic *= kpa_to_mmhg
        map_v *= kpa_to_mmhg

    return BPReading(
        systolic=systolic,
        diastolic=diastolic,
        map=map_v,
        pulse=pulse,
        timestamp_iso=ts_iso
    )


class MqttPublisher:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.client = mqtt.Client()
        if username:
            self.client.username_pw_set(username, password or None)
        self.host = host
        self.port = port
        self._connected = asyncio.Event()

        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        def on_connect(_client, _userdata, _flags, rc):
            print(f"[mqtt] connected rc={rc}")
            if rc == 0:
                self._connected.set()

        def on_disconnect(_client, _userdata, rc):
            print(f"[mqtt] disconnected rc={rc}")
            self._connected.clear()

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect

    def connect(self):
        print(f"[mqtt] connecting to {self.host}:{self.port} ...")
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()

    async def wait_connected(self, timeout: int = 10):
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    def publish_json(self, topic: str, payload: dict, retain: bool):
        self.client.publish(topic, json.dumps(payload), retain=retain)

    def publish_str(self, topic: str, payload: str, retain: bool):
        self.client.publish(topic, payload, retain=retain)


def publish_discovery(mq: MqttPublisher, discovery_prefix: str, device_id: str, device_name: str, base_topic: str, mac: str):
    """
    MQTT Discovery for 3 sensors:
      - systolic (mmHg) device_class=pressure
      - diastolic (mmHg) device_class=pressure
      - pulse (bpm) no device_class (to avoid HA weirdness with bpm)
    """
    state_topic = f"{base_topic}/{device_id}/state"
    availability_topic = f"{base_topic}/{device_id}/availability"

    device_block = {
        "identifiers": [device_id],
        "name": device_name,
        "manufacturer": "A&D",
        "model": "AND-UA-BLE",
        "connections": [["mac", mac]],
    }

    sensors = [
        ("systolic", "Systolic", "mmHg", "pressure", "{{ value_json.systolic }}"),
        ("diastolic", "Diastolic", "mmHg", "pressure", "{{ value_json.diastolic }}"),
        ("pulse", "Pulse", "bpm", None, "{{ value_json.pulse }}"),
    ]

    for key, name, unit, device_class, template in sensors:
        topic = f"{discovery_prefix}/sensor/{device_id}/{key}/config"
        payload = {
            "name": f"{device_name} {name}",
            "unique_id": f"{device_id}_{key}_v2",  # bump to refresh HA entities
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "unit_of_measurement": unit,
            "state_class": "measurement",
            "value_template": template,
            "device": device_block,
        }
        if device_class:
            payload["device_class"] = device_class

        mq.publish_json(topic, payload, retain=True)

    mq.publish_str(availability_topic, "online", retain=True)


async def wait_for_device(address: str, timeout: int = DEFAULT_SCAN_TIMEOUT):
    """Scan until device appears (prevents futile connect attempts while it sleeps)."""
    print(f"[ble] scanning for {address} (timeout {timeout}s) ...")
    dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
    if dev is None:
        raise RuntimeError("Device not found during scan")
    return dev


async def safe_start_notify(client: BleakClient, uuid: str, cb, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            await client.start_notify(uuid, cb)
            return
        except Exception as e:
            last = e
            print(f"[ble] start_notify failed ({i+1}/{tries}) {uuid}: {e}")
            await asyncio.sleep(1.0)
    raise last


async def run_reader(args):
    args.address = normalize_mac(args.address)

    device_id = mac_to_id(args.address)
    state_topic = f"{args.base_topic}/{device_id}/state"
    events_topic = f"{args.base_topic}/{device_id}/events"
    raw_topic = f"{args.base_topic}/{device_id}/raw"
    availability_topic = f"{args.base_topic}/{device_id}/availability"

    print(
        f"[start] address={args.address} mqtt={args.mqtt_host}:{args.mqtt_port} "
        f"base_topic={args.base_topic} publish_raw={args.publish_raw} final_only={args.final_only}"
    )

    # MQTT
    mq = MqttPublisher(args.mqtt_host, args.mqtt_port, args.mqtt_username, args.mqtt_password)
    mq.connect()
    await mq.wait_connected(10)

    publish_discovery(mq, args.discovery_prefix, device_id, args.device_name, args.base_topic, args.address)

    # For "final_only" publish after quiet period
    debounce_task: Optional[asyncio.Task] = None
    last_payload: Optional[dict] = None

    # Dedup events so we don't store the same measurement multiple times
    last_event_key: Optional[Tuple[str, float, float, float]] = None

    async def publish_after_quiet(delay: float):
        """Publish the last payload after a short quiet period, then emit an event."""
        nonlocal last_payload, last_event_key
        await asyncio.sleep(delay)
        if not last_payload:
            return

        # Retained "last known" state for HA sensors
        mq.publish_json(state_topic, last_payload, retain=True)
        print(f"[pub] state {last_payload}")

        # Non-retained event stream (for history)
        event = {
            "measurement_ts": last_payload.get("timestamp"),
            "received_ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "systolic": last_payload["systolic"],
            "diastolic": last_payload["diastolic"],
            "map": last_payload.get("map"),
            "pulse": last_payload.get("pulse"),
        }
        event_key = (str(event["measurement_ts"]), float(event["systolic"]), float(event["diastolic"]), float(event["pulse"]))
        if event_key != last_event_key:
            mq.publish_json(events_topic, event, retain=False)
            last_event_key = event_key
            print(f"[event] {event}")
        else:
            print("[event] duplicate skipped")

    while True:
        try:
            # Wait for device to appear (important for sleeping BLE devices)
            await wait_for_device(args.address, timeout=args.scan_timeout)

            print(f"[ble] Connecting to {args.address} ...")
            async with BleakClient(args.address, timeout=20.0) as client:
                print("[ble] Connected. Subscribing to BP measurement...")

                last_rx_ts = time.time()

                def on_bp(_char, data: bytearray):
                    nonlocal last_rx_ts, last_payload, debounce_task, last_event_key
                    b = bytes(data)
                    last_rx_ts = time.time()

                    if args.publish_raw:
                        mq.publish_str(raw_topic, b.hex(), retain=False)

                    # Parse; ignore intermediate frames silently
                    try:
                        r = parse_bp_measurement(b)
                    except Exception:
                        return

                    payload = {
                        "systolic": round(r.systolic, 1),
                        "diastolic": round(r.diastolic, 1),
                        "map": round(r.map, 1),
                        "pulse": (round(r.pulse, 1) if r.pulse is not None else None),
                        "timestamp": r.timestamp_iso or time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }

                    # Keep pulse numeric for HA pulse sensor; skip frames without pulse
                    if payload["pulse"] is None:
                        return

                    # Store last valid payload
                    last_payload = payload

                    if not args.final_only:
                        # Publish every valid frame as state + event (non-retained)
                        mq.publish_json(state_topic, payload, retain=True)
                        print(f"[bp] {payload}")

                        event = {
                            "measurement_ts": payload.get("timestamp"),
                            "received_ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "systolic": payload["systolic"],
                            "diastolic": payload["diastolic"],
                            "map": payload.get("map"),
                            "pulse": payload.get("pulse"),
                        }
                        event_key = (str(event["measurement_ts"]), float(event["systolic"]), float(event["diastolic"]), float(event["pulse"]))
                        if event_key != last_event_key:
                            mq.publish_json(events_topic, event, retain=False)
                            last_event_key = event_key
                            print(f"[event] {event}")
                        return

                    # final_only: debounce publishing (publish once per measurement)
                    if debounce_task and not debounce_task.done():
                        debounce_task.cancel()
                    debounce_task = asyncio.create_task(publish_after_quiet(args.final_quiet_seconds))

                await safe_start_notify(client, BP_MEASUREMENT_CHAR, on_bp, tries=3)
                print("[ble] subscribed, waiting for measurement...")

                mq.publish_str(availability_topic, "online", retain=True)

                # Heartbeat + watchdog: reconnect if the device is silent for too long
                while True:
                    await asyncio.sleep(5)
                    idle = time.time() - last_rx_ts
                    print(f"[tick] connected idle={int(idle)}s")
                    if idle > args.idle_watchdog:
                        raise RuntimeError(f"watchdog: no notifications for {args.idle_watchdog}s")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[ble] Error: {e}. Reconnecting in {args.reconnect_seconds}s")
            mq.publish_str(availability_topic, "offline", retain=True)
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
    ap.add_argument("--base-topic", default="anduable")
    ap.add_argument("--device-name", default="AND-UA-BLE")
    ap.add_argument("--reconnect-seconds", type=int, default=10)

    ap.add_argument("--publish-raw", action="store_true")

    # Stabilization controls
    ap.add_argument("--final-only", action="store_true")
    ap.add_argument("--final-quiet-seconds", type=float, default=2.0)
    ap.add_argument("--idle-watchdog", type=int, default=DEFAULT_IDLE_WATCHDOG)
    ap.add_argument("--scan-timeout", type=int, default=DEFAULT_SCAN_TIMEOUT)

    args = ap.parse_args()
    asyncio.run(run_reader(args))


if __name__ == "__main__":
    main()
