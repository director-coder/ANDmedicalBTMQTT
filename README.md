# UA-BLE → MQTT (Home Assistant Add-on)

A Home Assistant OS (HAOS) add-on that connects to A&D BLE blood pressure monitors  
(e.g. **UA-651BLE / UA-911BLE**), receives measurement results and publishes them to MQTT.

The add-on provides:
- **Latest measurement** → MQTT `state` topic (retain=true) for Home Assistant MQTT Discovery sensors
- **All measurements** → MQTT `events` topic (retain=false) as an event stream
- **Stable BLE handling** for sleeping / intermittent devices
- **MQTT LWT-based availability** (sensors do not become UNAVAILABLE between measurements)
- A dedicated **BLE status sensor** for diagnostics

---

## Features

- Automatic MQTT Discovery for Home Assistant
  - Sensors: **Systolic**, **Diastolic**, **Pulse**
- Event stream with **all measurements** (including measurements stored in device memory)
- Designed for intermittent BLE devices:
  - Scan-before-connect
  - Retry on notify subscription
  - Idle watchdog with reconnect
- MQTT **LWT (Last Will and Testament)** for correct availability handling
- Separate BLE status sensor (`ble_status`)
- Optional **final-only mode** (publish only the final measurement result)

---

## Installation (HAOS)

1. Go to **Settings → Add-ons → Add-on Store**
2. Open the menu (⋮) → **Repositories**
3. Add your custom add-on repository URL
4. Find the add-on → **Install**
5. Open **Configuration**, set the options (see below)
6. **Start** the add-on

> ⚠️ BLE pairing is usually done with a separate *PAIR* add-on included here.  
> This add-on assumes the device is already paired and trusted in BlueZ.

---

## Configuration

Configured via Home Assistant UI:  
**Settings → Add-ons → UA-BLE MQTT → Configuration**

---
