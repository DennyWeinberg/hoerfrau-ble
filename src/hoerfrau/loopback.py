"""In-process transport: plugs hoermoles_ble's HoermannClient straight into a
SimulatedDrive, no radio involved. For tests, here and in the Home Assistant
integration:

    drive = SimulatedDrive(DriveState.create(key_size=1024))
    async with HoermannClient(LoopbackTransport(drive)) as client:
        await client.wait_for_any_notification()
        creds = await client.register(drive.qr_text, "AA:BB:CC:DD:EE:FF")
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from hoermoles_ble.transport import BleTransport

from .drive import DriveSession, SimulatedDrive


class LoopbackTransport(BleTransport):
    def __init__(self, drive: SimulatedDrive):
        self._drive = drive
        self._session: DriveSession | None = None
        self._callback: Callable[[bytes], None] | None = None

    async def connect(self) -> None:
        self._session = DriveSession(self._drive, self._deliver, name="loopback")

    async def disconnect(self) -> None:
        self._session = None
        self._callback = None

    async def write(self, data: bytes) -> None:
        if self._session is None:
            raise ConnectionError("not connected")
        self._session.receive(bytes(data))

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        if self._session is None:
            raise ConnectionError("not connected")
        self._callback = callback
        self._session.start()

    async def stop_notify(self) -> None:
        self._callback = None

    def _deliver(self, data: bytes) -> None:
        # Never call back synchronously from inside write(): over real BLE a
        # notification always arrives after the write returned, and the client
        # relies on that ordering (it sets up its collectors after writing).
        # call_soon keeps the chunks in order.
        asyncio.get_running_loop().call_soon(self._fire, data)

    def _fire(self, data: bytes) -> None:
        if self._callback is not None:
            self._callback(data)
