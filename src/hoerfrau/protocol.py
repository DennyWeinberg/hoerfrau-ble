"""Drive-side view of the Hoermann BlueSecur BLE protocol.

Written independently from the drive's perspective rather than importing
hoermoles_ble.protocol: the simulator is a test target for that library, so
sharing its code would let a bug on one side cancel out on the other. The
interop tests run the real hoermoles_ble client against this module instead.

Reference: hoermoles-ble/python/packages/hoermoles-ble/src/hoermoles_ble/protocol.py,
advertisement.py and device_log.py.

Wire format, both directions:
    [io_id(1)][length(2 LE, including these 3 bytes)][body]

Client -> drive, io_id=1 (Signed):
    [root_id(2)][command(2)][length(2, = len(body))][payload][HMAC-SHA256(32)]
    signature = HMAC(key, header + payload + current challenge)
Client -> drive, io_id=2 (Encrypted, registration only):
    [RSA-PKCS1v1.5(register_key(32))]
Drive -> client, io_id=1:
    [challenge(8)][type(2)][reserved(2)][payload]
Drive -> client, io_id=2:
    [status(1)]  1=continue, 2=complete, 3=error
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

BC_SERVICE = "669a9001-0008-968f-e311-6050405558b3"
BC_TX = "669a900c-0008-968f-e311-6050405558b3"  # client writes here
BC_RX = "669a900a-0008-968f-e311-6050405558b3"  # drive notifies here

COMPANY_ID = 1972  # 0x07B4, manufacturer-specific advertisement data

CHUNK_SIZE = 20  # default ATT MTU payload; the drive notifies in chunks of this size

IO_SIGNED = 0x01
IO_ENCRYPTED = 0x02
IO_BLUECONTROL = 0x03

ENCRYPTED_ACK_CONTINUE = 1
ENCRYPTED_ACK_COMPLETE = 2
ENCRYPTED_ACK_ERROR = 3

# Command ids (lower 16 bits of DeviceAction)
CMD_REGISTER_ROOT = 0x0001
CMD_CHANNEL_FIRST = 0x0011  # CHANNEL_1 .. CHANNEL_6 = 0x11 .. 0x16
CMD_CHANNEL_LAST = 0x0016
CMD_GET_LOG = 0x0021
CMD_GET_PROPERTIES = 0x0028
CMD_SET_PROPERTIES = 0x0029
CMD_GET_SELECTED_PROPERTIES = 0x002C
CMD_READ_SERVICE_DATA = 0x0042

COMMAND_NAMES = {
    CMD_REGISTER_ROOT: "REGISTER_ROOT",
    **{CMD_CHANNEL_FIRST + n: f"CHANNEL_{n + 1}" for n in range(6)},
    CMD_GET_LOG: "GET_LOG",
    CMD_GET_PROPERTIES: "GET_PROPERTIES",
    CMD_SET_PROPERTIES: "SET_PROPERTIES",
    CMD_GET_SELECTED_PROPERTIES: "GET_SELECTED_PROPERTIES",
    CMD_READ_SERVICE_DATA: "READ_SERVICE_DATA",
}

# Signed notification types
NOTIF_ROOT_KEY = 2
NOTIF_ENABLED = 4
NOTIF_LOG = 6
NOTIF_LOG_END = 7
NOTIF_PROPERTIES_LIST = 16
NOTIF_PROPERTIES_LIST_END = 17
NOTIF_PROPERTY_ACCEPTED = 18
NOTIF_PROPERTIES_INVALID = 19
NOTIF_SERVICE_DATA = 27
NOTIF_SERVICE_DATA_END = 28

# Audit log tags
LOG_REGISTER_ROOT = 1
LOG_IMPULS_WITH_CLOCK = 9

SIZE_SIGNATURE = 32
SIZE_ROOT_KEY = 32
SIZE_CHALLENGE = 8
SIGNED_HEADER = struct.Struct("<HHH")

LOG_EPOCH_UNIX = 946684800  # 2000-01-01T00:00:00Z, log timestamps count from here


def envelope(io_id: int, body: bytes) -> bytes:
    return bytes([io_id]) + struct.pack("<H", len(body) + 3) + body


def chunks(data: bytes, size: int = CHUNK_SIZE) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def signed_notification(challenge: bytes, notif_type: int, payload: bytes = b"") -> bytes:
    return envelope(IO_SIGNED, challenge + struct.pack("<HH", notif_type, 0) + payload)


def encrypted_ack(status: int) -> bytes:
    return envelope(IO_ENCRYPTED, bytes([status]))


class FrameAssembler:
    """Reassembles client writes (arriving in <=20-byte chunks) into complete
    (io_id, body) frames. Garbage that doesn't start a valid frame is dropped."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def pending(self) -> bool:
        return bool(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buffer.extend(data)
        frames = []
        while len(self._buffer) >= 3:
            io_id = self._buffer[0]
            length = struct.unpack_from("<H", self._buffer, 1)[0]
            if io_id not in (IO_SIGNED, IO_ENCRYPTED, IO_BLUECONTROL) or length < 3:
                self._buffer.clear()
                break
            if len(self._buffer) < length:
                break
            frames.append((io_id, bytes(self._buffer[3:length])))
            del self._buffer[:length]
        return frames


@dataclass
class SignedCommand:
    root_id: int
    command: int
    payload: bytes
    signature: bytes
    signed_part: bytes  # header + payload, i.e. what the HMAC covers besides the challenge

    @property
    def name(self) -> str:
        return COMMAND_NAMES.get(self.command, f"0x{self.command:04x}")

    @classmethod
    def parse(cls, body: bytes) -> SignedCommand:
        if len(body) < SIGNED_HEADER.size + SIZE_SIGNATURE:
            raise ValueError(f"signed frame too short ({len(body)} bytes)")
        root_id, command, length = SIGNED_HEADER.unpack_from(body)
        if length != len(body):
            raise ValueError(f"signed length field {length} != body length {len(body)}")
        split = len(body) - SIZE_SIGNATURE
        return cls(
            root_id=root_id,
            command=command,
            payload=body[SIGNED_HEADER.size : split],
            signature=body[split:],
            signed_part=body[:split],
        )

    def verify(self, key: bytes, challenge: bytes) -> bool:
        expected = hmac.new(key, self.signed_part + challenge, hashlib.sha256).digest()
        return hmac.compare_digest(expected, self.signature)


def properties_payload(items: list[tuple[int, int]]) -> bytes:
    """[count] + count * [menu_group(1), value(int16 LE)]"""
    return bytes([len(items)]) + b"".join(bytes([group]) + struct.pack("<h", value) for group, value in items)


def service_data_payload(items: list[tuple[int, int]]) -> bytes:
    """[count] + count * [value(uint32 LE), service_type(1)] - value first, unlike properties."""
    return bytes([len(items)]) + b"".join(struct.pack("<I", value) + bytes([kind]) for kind, value in items)


def log_entry_payload(tag: int, timestamp: int, data: bytes) -> bytes:
    """[data_length][tag][timestamp(uint32 LE, seconds since 2000)][data]"""
    return bytes([len(data), tag]) + struct.pack("<I", timestamp) + data
