"""BLE peripheral on macOS via CoreBluetooth (pyobjc).

Exposes the drive's BlueConnect GATT service (TX write / RX notify) so any BLE
central - Home Assistant on another machine, the hoermoles-ble CLI or web app
on another computer or phone - can talk to the simulator like to a real drive.

macOS limits, and what they mean for testing:

- CBPeripheralManager can only advertise a local name and service UUIDs, NOT
  manufacturer data. The real drive's 1972 manufacturer packets therefore never
  go out over the air; the simulator advertises `HFSIM<serial tail>` plus the
  BlueConnect service UUID instead, and serves the would-be manufacturer
  packets from a debug characteristic (DEBUG_ADV, JSON) for anything that
  needs them.
- A peripheral cannot disconnect a central. The real drive drops the link right
  after a command; here the client has to disconnect itself.
- macOS advertises from a private address of its own choosing, which may rotate.

CoreBluetooth calls the delegate on its own dispatch queue; everything is
handed over to the asyncio loop with call_soon_threadsafe and all drive logic
runs there.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque

import objc
from CoreBluetooth import (
    CBAdvertisementDataLocalNameKey,
    CBAdvertisementDataServiceUUIDsKey,
    CBAttributePermissionsReadable,
    CBAttributePermissionsWriteable,
    CBATTErrorInvalidOffset,
    CBATTErrorSuccess,
    CBCharacteristicPropertyNotify,
    CBCharacteristicPropertyRead,
    CBCharacteristicPropertyWrite,
    CBCharacteristicPropertyWriteWithoutResponse,
    CBManagerStatePoweredOn,
    CBManagerStateUnauthorized,
    CBManagerStateUnsupported,
    CBMutableCharacteristic,
    CBMutableService,
    CBPeripheralManager,
    CBUUID,
)
from Foundation import NSData, NSObject
from libdispatch import DISPATCH_QUEUE_SERIAL, dispatch_queue_create

from . import protocol as p
from .drive import DriveSession, SimulatedDrive

log = logging.getLogger("hoerfrau.ble")

# Not part of the real drive: a simulator-only service for test tooling.
# "686f6572-6672-6175" is "hoerfrau" in ASCII hex.
DEBUG_SERVICE = "686f6572-6672-6175-0001-000000000000"
DEBUG_ADV = "686f6572-6672-6175-0002-000000000000"

CBPeripheralManagerDelegate = objc.protocolNamed("CBPeripheralManagerDelegate")


def _nsdata(data: bytes) -> NSData:
    return NSData.dataWithBytes_length_(data, len(data))


class _Delegate(NSObject, protocols=[CBPeripheralManagerDelegate]):
    """Thin trampoline: every callback is forwarded to the asyncio loop."""

    def initWithOwner_(self, owner):  # noqa: N802
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    @objc.python_method
    def _forward(self, method, *args):
        self.owner.loop.call_soon_threadsafe(method, *args)

    def peripheralManagerDidUpdateState_(self, manager):  # noqa: N802
        self._forward(self.owner._on_state, manager.state())

    def peripheralManager_didAddService_error_(self, manager, service, error):  # noqa: N802
        self._forward(self.owner._on_service_added, service.UUID().UUIDString(), error)

    def peripheralManagerDidStartAdvertising_error_(self, manager, error):  # noqa: N802
        self._forward(self.owner._on_advertising, error)

    def peripheralManager_central_didSubscribeToCharacteristic_(self, manager, central, characteristic):  # noqa: N802
        self._forward(self.owner._on_subscribe, central, characteristic.UUID().UUIDString().lower())

    def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, manager, central, characteristic):  # noqa: N802
        self._forward(self.owner._on_unsubscribe, central, characteristic.UUID().UUIDString().lower())

    def peripheralManagerIsReadyToUpdateSubscribers_(self, manager):  # noqa: N802
        self._forward(self.owner._ready.set)

    def peripheralManager_didReceiveReadRequest_(self, manager, request):  # noqa: N802
        # Answered right here on the CoreBluetooth queue: it reads one immutable
        # bytes object that the loop thread swaps out atomically.
        value = self.owner.debug_value
        offset = request.offset()
        if offset > len(value):
            manager.respondToRequest_withResult_(request, CBATTErrorInvalidOffset)
            return
        request.setValue_(_nsdata(value[offset:]))
        manager.respondToRequest_withResult_(request, CBATTErrorSuccess)

    def peripheralManager_didReceiveWriteRequests_(self, manager, requests):  # noqa: N802
        for request in requests:
            data = bytes(request.value() or b"")
            self._forward(self.owner._on_write, request.central(), request.characteristic().UUID().UUIDString(), data)
        # Needed for write-with-response, harmless for write-without-response.
        manager.respondToRequest_withResult_(requests[0], CBATTErrorSuccess)


class MacPeripheral:
    def __init__(self, drive: SimulatedDrive, local_name: str):
        self.drive = drive
        self.local_name = local_name
        self.loop = asyncio.get_running_loop()
        self.debug_value = b""
        self._sessions: dict[str, DriveSession] = {}
        self._debug_subscribers: dict[str, object] = {}
        self._outbox: deque[tuple[object, object, bytes]] = deque()
        self._ready = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._state_future: asyncio.Future[int] = self.loop.create_future()
        self._pending_services: dict[str, asyncio.Future[None]] = {}
        self._advertising_future: asyncio.Future[None] | None = None
        self._pump_task: asyncio.Task | None = None

        self._delegate = _Delegate.alloc().initWithOwner_(self)
        self._manager = CBPeripheralManager.alloc().initWithDelegate_queue_(
            self._delegate, dispatch_queue_create(b"hoerfrau.ble", DISPATCH_QUEUE_SERIAL)
        )

        self._tx = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            CBUUID.UUIDWithString_(p.BC_TX),
            CBCharacteristicPropertyWrite | CBCharacteristicPropertyWriteWithoutResponse,
            None,
            CBAttributePermissionsWriteable,
        )
        self._rx = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            CBUUID.UUIDWithString_(p.BC_RX), CBCharacteristicPropertyNotify, None, CBAttributePermissionsReadable
        )
        self._adv = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            CBUUID.UUIDWithString_(DEBUG_ADV),
            CBCharacteristicPropertyRead | CBCharacteristicPropertyNotify,
            None,
            CBAttributePermissionsReadable,
        )
        self.refresh_debug_value()

    # --- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        state = await asyncio.wait_for(self._state_future, timeout=10)
        if state == CBManagerStateUnauthorized:
            raise RuntimeError(
                "Bluetooth access denied. Allow it for your terminal/IDE in "
                "System Settings > Privacy & Security > Bluetooth, then restart it."
            )
        if state == CBManagerStateUnsupported:
            raise RuntimeError("This Mac does not support the BLE peripheral role.")
        if state != CBManagerStatePoweredOn:
            raise RuntimeError(f"Bluetooth is not powered on (CBManagerState={state}).")

        await self._add_service(p.BC_SERVICE, [self._tx, self._rx])
        await self._add_service(DEBUG_SERVICE, [self._adv])

        self._advertising_future = self.loop.create_future()
        self._manager.startAdvertising_(
            {
                CBAdvertisementDataLocalNameKey: self.local_name,
                CBAdvertisementDataServiceUUIDsKey: [CBUUID.UUIDWithString_(p.BC_SERVICE)],
            }
        )
        await asyncio.wait_for(self._advertising_future, timeout=5)
        self._pump_task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        self._manager.stopAdvertising()
        self._manager.removeAllServices()
        if self._pump_task is not None:
            self._pump_task.cancel()

    async def _add_service(self, uuid: str, characteristics: list) -> None:
        service = CBMutableService.alloc().initWithType_primary_(CBUUID.UUIDWithString_(uuid), True)
        service.setCharacteristics_(characteristics)
        future = self.loop.create_future()
        self._pending_services[uuid.upper()] = future
        self._manager.addService_(service)
        await asyncio.wait_for(future, timeout=5)

    # --- CoreBluetooth events (on the loop thread) ------------------------------------

    def _on_state(self, state: int) -> None:
        if not self._state_future.done():
            self._state_future.set_result(state)
        elif state != CBManagerStatePoweredOn:
            log.error("Bluetooth state changed to %d - the simulator is offline until it is back on", state)

    def _on_service_added(self, uuid: str, error) -> None:
        future = self._pending_services.pop(uuid.upper(), None)
        if future is None or future.done():
            return
        if error is not None:
            future.set_exception(RuntimeError(f"addService {uuid} failed: {error}"))
        else:
            future.set_result(None)

    def _on_advertising(self, error) -> None:
        if self._advertising_future is None or self._advertising_future.done():
            return
        if error is not None:
            self._advertising_future.set_exception(RuntimeError(f"startAdvertising failed: {error}"))
        else:
            self._advertising_future.set_result(None)

    def _on_subscribe(self, central, char_uuid: str) -> None:
        central_id = central.identifier().UUIDString()
        if char_uuid == DEBUG_ADV:
            self._debug_subscribers[central_id] = central
            return
        if char_uuid != p.BC_RX:
            return
        log.info("Central %s connected and subscribed (max notify size %d)", central_id[:8], central.maximumUpdateValueLength())
        session = self._session_for(central)
        session.start()

    def _on_unsubscribe(self, central, char_uuid: str) -> None:
        central_id = central.identifier().UUIDString()
        if char_uuid == DEBUG_ADV:
            self._debug_subscribers.pop(central_id, None)
        elif char_uuid == p.BC_RX and self._sessions.pop(central_id, None) is not None:
            log.info("Central %s unsubscribed / disconnected", central_id[:8])

    def _on_write(self, central, char_uuid: str, data: bytes) -> None:
        if char_uuid.lower() != p.BC_TX:
            return
        log.debug("RX %s <- %s", central.identifier().UUIDString()[:8], data.hex())
        self._session_for(central).receive(data)

    def _session_for(self, central) -> DriveSession:
        central_id = central.identifier().UUIDString()
        session = self._sessions.get(central_id)
        if session is None:
            session = DriveSession(self.drive, lambda data: self._send(central, self._rx, data), name=central_id[:8])
            self._sessions[central_id] = session
        return session

    # --- notifications ----------------------------------------------------------------

    def refresh_debug_value(self) -> None:
        """Called on every drive change: the would-be advertisement as JSON, for
        reads of DEBUG_ADV, pushed to anyone subscribed to it."""
        short, long = self.drive.manufacturer_packets()
        doc = {"company_id": p.COMPANY_ID, "packets": [short.hex(), long.hex()], "serial_no": self.drive.state.serial_no}
        self.debug_value = json.dumps(doc, separators=(",", ":")).encode()
        for central in self._debug_subscribers.values():
            self._send(central, self._adv, self.debug_value)

    def _send(self, central, characteristic, data: bytes) -> None:
        self._outbox.append((central, characteristic, data))
        self._wakeup.set()

    async def _pump(self) -> None:
        """updateValue returns False when CoreBluetooth's transmit queue is full;
        then wait for peripheralManagerIsReadyToUpdateSubscribers and retry."""
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while self._outbox:
                central, characteristic, data = self._outbox[0]
                self._ready.clear()
                sent = self._manager.updateValue_forCharacteristic_onSubscribedCentrals_(
                    _nsdata(data), characteristic, [central]
                )
                if sent:
                    self._outbox.popleft()
                    if characteristic is self._rx:
                        log.debug("TX %s -> %s", central.identifier().UUIDString()[:8], data.hex())
                else:
                    try:
                        await asyncio.wait_for(self._ready.wait(), timeout=2)
                    except TimeoutError:
                        log.warning("Dropping a notification for %s: central gone?", central.identifier().UUIDString()[:8])
                        self._outbox.popleft()
