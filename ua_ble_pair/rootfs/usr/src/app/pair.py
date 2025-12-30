import argparse
import asyncio
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method, dbus_property
from dbus_next import Variant
from dbus_next.constants import BusType
from dbus_next import PropertyAccess

BLUEZ = "org.bluez"
OBJMGR_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
AGENTMGR_IFACE = "org.bluez.AgentManager1"
AGENT_IFACE = "org.bluez.Agent1"


class Agent(ServiceInterface):
    """
    Минимальный агент: подтверждает pairing без ввода.
    Для большинства BLE-устройств достаточно NoInputNoOutput.
    """
    def __init__(self):
        super().__init__(AGENT_IFACE)

    @method()
    def Release(self):
        print("[agent] Release")

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        print(f"[agent] RequestAuthorization for {device} -> ok")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
        print(f"[agent] AuthorizeService {uuid} for {device} -> ok")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        print(f"[agent] RequestConfirmation passkey={passkey} for {device} -> confirm")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        print(f"[agent] DisplayPasskey passkey={passkey} entered={entered} for {device}")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
        print(f"[agent] DisplayPinCode pincode={pincode} for {device}")

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        # Если устройство внезапно потребует passkey, возвращаем 0
        print(f"[agent] RequestPasskey for {device} -> 0")
        return 0

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        print(f"[agent] RequestPinCode for {device} -> '0000'")
        return "0000"

    @method()
    def Cancel(self):
        print("[agent] Cancel")

    @dbus_property(access=PropertyAccess.READ)
    def Capabilities(self) -> "s":              #noqa: F821
        return "NoInputNoOutput"


async def get_managed_objects(bus: MessageBus):
    introspection = await bus.introspect(BLUEZ, "/")
    obj = bus.get_proxy_object(BLUEZ, "/", introspection)
    mgr = obj.get_interface(OBJMGR_IFACE)
    return await mgr.call_get_managed_objects()


def find_adapter_path(objects: dict, adapter_name: str) -> str:
    # adapter_name обычно "hci0"
    for path, ifaces in objects.items():
        if ADAPTER_IFACE in ifaces and path.endswith("/" + adapter_name):
            return path
    raise RuntimeError(f"Adapter {adapter_name} not found in BlueZ")


def find_device_path(objects: dict, address: str, name_hint: str) -> str | None:
    addr_norm = address.strip().upper()
    for path, ifaces in objects.items():
        dev = ifaces.get(DEVICE_IFACE)
        if not dev:
            continue
        props = dev
        # props — dict[str, Variant]
        addr = props.get("Address")
        name = props.get("Name")
        if addr and addr.value.upper() == addr_norm:
            return path
        if (not addr_norm) and name and name_hint and name_hint.lower() in str(name.value).lower():
            return path
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default="")
    ap.add_argument("--name-hint", default="UA-BLE")
    ap.add_argument("--adapter", default="hci0")
    ap.add_argument("--discover-seconds", type=int, default=30)
    args = ap.parse_args()

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # 1) Регистрируем Agent
    agent_path = "/anduable/agent"
    agent = Agent()
    bus.export(agent_path, agent)

    am_intro = await bus.introspect(BLUEZ, "/org/bluez")
    am_obj = bus.get_proxy_object(BLUEZ, "/org/bluez", am_intro)
    agent_mgr = am_obj.get_interface(AGENTMGR_IFACE)

    print("[pair] RegisterAgent + RequestDefaultAgent")
    await agent_mgr.call_register_agent(agent_path, "NoInputNoOutput")
    await agent_mgr.call_request_default_agent(agent_path)

    # 2) Находим адаптер
    objects = await get_managed_objects(bus)

# DEBUG: показать всё, что видим во время сканирования
    for path, ifaces in objects.items():
        dev = ifaces.get(DEVICE_IFACE)
        if not dev:
            continue
        addr = dev.get("Address")
        name = dev.get("Name")
        rssi = dev.get("RSSI")
        if addr and name:
            print(f"[scan] {addr.value}  {name.value}  rssi={getattr(rssi,'value',None)}  path={path}")

    adapter_path = find_adapter_path(objects, args.adapter)

    ad_intro = await bus.introspect(BLUEZ, adapter_path)
    ad_obj = bus.get_proxy_object(BLUEZ, adapter_path, ad_intro)
    adapter = ad_obj.get_interface(ADAPTER_IFACE)

    # 3) Discovery
    print(f"[pair] StartDiscovery ({args.discover_seconds}s)")
    try:
        await adapter.call_start_discovery()
    except Exception as e:
        print(f"[pair] StartDiscovery error (ignored if already discovering): {e}")

    device_path = None
    for _ in range(max(1, args.discover_seconds)):
        await asyncio.sleep(1)
        objects = await get_managed_objects(bus)
        device_path = find_device_path(objects, args.address, args.name_hint)
        if device_path:
            break

    # 4) StopDiscovery
    try:
        await adapter.call_stop_discovery()
    except Exception as e:
        print(f"[pair] StopDiscovery error (ignored): {e}")

    if not device_path:
        raise RuntimeError("Device not found. Put UA-BLE into pairing mode and try again.")

    print(f"[pair] Found device: {device_path}")

    # 5) Pair + Trust
    dev_intro = await bus.introspect(BLUEZ, device_path)
    dev_obj = bus.get_proxy_object(BLUEZ, device_path, dev_intro)
    dev = dev_obj.get_interface(DEVICE_IFACE)
    props = dev_obj.get_interface("org.freedesktop.DBus.Properties")

    paired = await props.call_get(DEVICE_IFACE, "Paired")
    if paired.value:
        print("[pair] Already paired")
    else:
        print("[pair] Pair() ...")
        await dev.call_pair()
        print("[pair] Paired OK")

    print("[pair] Set Trusted=true")
    await props.call_set(DEVICE_IFACE, "Trusted", Variant("b", True))

    print("[pair] Done. You can now run the MQTT reader add-on.")
    # Важно: после пары можно просто выйти
    await agent_mgr.call_unregister_agent(agent_path)


if __name__ == "__main__":
    asyncio.run(main())
