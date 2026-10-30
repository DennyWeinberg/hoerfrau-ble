"""The simulated drive: identity, persisted state, door mechanics, and the
per-connection protocol session.

Everything here is transport-agnostic. A `DriveSession` gets raw written
chunks via `receive()` and emits raw notification chunks through the `send`
callable it was created with - the macOS GATT server and the in-process
loopback transport both plug in at that seam.

Where the real drive's behaviour is unknown, the choice made here is marked
ASSUMPTION so it is easy to revisit once compared against hardware.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from hoermoles_ble.menu_settings import menu_table_for_product

from . import protocol as p

log = logging.getLogger("hoerfrau")

PRODUCT_CLASS_SUPRAMATIC = 2
PRODUCT_ID_SUPRAMATIC_E4 = 2

POSITION_MAX = 200  # advertisement unit: half percent
POSITION_PARTIAL = 24  # 12% - live-observed "partial" opening on a real S4
POSITION_VENTILATION = 8  # 4% - live-observed "ventilation" opening

CHANNEL_NAMES = {1: "impulse", 2: "light", 3: "partial", 4: "open", 5: "close", 6: "ventilation"}

FRAME_TIMEOUT = 1.0  # discard a half-received write after this many seconds
PROPERTIES_PER_NOTIFICATION = 5
SERVICE_DATA_PER_NOTIFICATION = 3
LOG_CAPACITY = 64


def _menu_settings():
    return menu_table_for_product(PRODUCT_CLASS_SUPRAMATIC, PRODUCT_ID_SUPRAMATIC_E4).settings


def _default_properties() -> dict[int, int]:
    """Factory defaults of every Supramatic E4 menu, from hoermoles_ble's menu table."""
    values = {}
    for setting in _menu_settings():
        default = next((param.value for param in setting.parameters if param.is_default), None)
        if default is None and setting.parameters:
            default = setting.parameters[0].value
        values[setting.menu_group] = default if default is not None else 0
    return values


def _valid_property_values() -> dict[int, set[int] | None]:
    return {setting.menu_group: ({param.value for param in setting.parameters} or None) for setting in _menu_settings()}


@dataclass
class Admin:
    root_id: int
    root_key: bytes
    username: str


@dataclass
class DriveState:
    """Everything that survives a simulator restart."""

    serial_no: int
    private_key_pem: str
    product_class: int = PRODUCT_CLASS_SUPRAMATIC
    product_id: int = PRODUCT_ID_SUPRAMATIC_E4
    admins: dict[int, Admin] = field(default_factory=dict)
    teachable: bool = True  # factory state: no admin yet, registration open
    next_root_id: int = 1
    position: int = 0
    light: bool = False
    low_battery: bool = False
    maintenance_required: bool = False
    clock_offset: float | None = None  # drive clock - host clock, None until a command set it
    log: list[tuple[int, int, str]] = field(default_factory=list)  # (tag, timestamp, data hex), newest first
    counters: dict[int, int] = field(default_factory=dict)
    properties: dict[int, int] = field(default_factory=dict)

    @classmethod
    def create(cls, serial_no: int | None = None, key_size: int = 2048) -> DriveState:
        key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        if serial_no is None:
            serial_no = secrets.randbelow(10**11) + 10**10
        now_log = int(time.time()) - p.LOG_EPOCH_UNIX
        counters = {0: 1, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: now_log, 7: 0, 8: 0, 9: 0, 10: 0}
        counters |= {11: 0, 12: 0, 13: 0, 14: 0, 15: 0, 16: 0, 17: 4}
        return cls(serial_no=serial_no, private_key_pem=pem, counters=counters, properties=_default_properties())

    def to_json(self) -> dict:
        return {
            "serial_no": self.serial_no,
            "product_class": self.product_class,
            "product_id": self.product_id,
            "private_key_pem": self.private_key_pem,
            "admins": [
                {"root_id": a.root_id, "root_key": a.root_key.hex(), "username": a.username}
                for a in self.admins.values()
            ],
            "teachable": self.teachable,
            "next_root_id": self.next_root_id,
            "position": self.position,
            "light": self.light,
            "low_battery": self.low_battery,
            "maintenance_required": self.maintenance_required,
            "clock_offset": self.clock_offset,
            "log": [list(entry) for entry in self.log],
            "counters": {str(k): v for k, v in self.counters.items()},
            "properties": {str(k): v for k, v in self.properties.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> DriveState:
        return cls(
            serial_no=data["serial_no"],
            product_class=data.get("product_class", PRODUCT_CLASS_SUPRAMATIC),
            product_id=data.get("product_id", PRODUCT_ID_SUPRAMATIC_E4),
            private_key_pem=data["private_key_pem"],
            admins={
                a["root_id"]: Admin(a["root_id"], bytes.fromhex(a["root_key"]), a["username"])
                for a in data.get("admins", [])
            },
            teachable=data.get("teachable", True),
            next_root_id=data.get("next_root_id", 1),
            position=data.get("position", 0),
            light=data.get("light", False),
            low_battery=data.get("low_battery", False),
            maintenance_required=data.get("maintenance_required", False),
            clock_offset=data.get("clock_offset"),
            log=[tuple(entry) for entry in data.get("log", [])],
            counters={int(k): v for k, v in data.get("counters", {}).items()},
            properties={int(k): v for k, v in data.get("properties", {}).items()} or _default_properties(),
        )

    @classmethod
    def load_or_create(cls, path: Path) -> DriveState:
        if path.exists():
            return cls.from_json(json.loads(path.read_text()))
        state = cls.create()
        state.save(path)
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2))
        os.chmod(tmp, 0o600)  # holds the drive's private key and the root keys
        tmp.replace(path)


class SimulatedDrive:
    def __init__(self, state: DriveState, state_path: Path | None = None, travel_time: float = 15.0):
        self.state = state
        self.state_path = state_path
        self.travel_time = travel_time  # seconds for a full close -> open run
        self.private_key = serialization.load_pem_private_key(state.private_key_pem.encode(), password=None)
        self.direction = 0  # +1 opening, -1 closing, 0 stopped
        self.last_direction = -1
        self.target: int | None = None
        self._exact_position = float(state.position)
        self._run_started_at: int | None = None
        self.on_change: Callable[[], None] = lambda: None
        self.state.counters[0] = self.state.counters.get(0, 0) + 1  # power-on counter

    # --- identity -------------------------------------------------------------

    @property
    def qr_prefix(self) -> str:
        """31 digits: version 03, class and id as 2 hex digits, 3 unknown digits,
        20-digit serial, 2 trailing digits - the layout of the one real label
        decoded so far ("0302020000030262602641451030700")."""
        return f"03{self.state.product_class:02X}{self.state.product_id:02X}000{self.state.serial_no:020d}00"

    @property
    def qr_text(self) -> str:
        """The QR sticker content: prefix + base64(RSA SubjectPublicKeyInfo DER)."""
        der = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return self.qr_prefix + base64.b64encode(der).decode()

    # --- admin / teach mode -----------------------------------------------------

    @property
    def admin_teached(self) -> bool:
        return bool(self.state.admins)

    def reset_admins(self) -> None:
        """Menu 19 / parameter 02 on the drive: forget every admin and accept a
        new registration."""
        self.state.admins.clear()
        self.state.teachable = True
        log.warning("Menu 19/02: all admins deleted, drive accepts a new registration (AdminsCanBeTeached=1)")
        self.changed()

    def add_admin(self, register_key: bytes, username: str) -> tuple[Admin, bytes]:
        """Returns the new admin and the 32 bytes to put on the wire
        (root_key XOR register_key)."""
        root_key = secrets.token_bytes(p.SIZE_ROOT_KEY)
        admin = Admin(self.state.next_root_id, root_key, username)
        self.state.next_root_id += 1
        self.state.admins[admin.root_id] = admin
        # ASSUMPTION: one registration per teach window, like a reset-then-pair flow.
        self.state.teachable = False
        self.add_log(p.LOG_REGISTER_ROOT, struct.pack("<H", admin.root_id))
        self.changed()
        return admin, bytes(a ^ b for a, b in zip(root_key, register_key))

    def decrypt_register_key(self, ciphertext: bytes) -> bytes | None:
        try:
            plain = self.private_key.decrypt(ciphertext, padding.PKCS1v15())
        except ValueError:
            return None
        # OpenSSL's implicit rejection returns random bytes instead of failing on
        # bad padding, so the length check is what catches a wrong key.
        return plain if len(plain) == p.SIZE_ROOT_KEY else None

    # --- clock and log ------------------------------------------------------------

    def clock(self) -> float:
        return time.time() + (self.state.clock_offset or 0.0)

    def set_clock(self, unix_time: int) -> tuple[int, int]:
        old = int(self.clock()) - p.LOG_EPOCH_UNIX
        self.state.clock_offset = unix_time - time.time()
        return old, unix_time - p.LOG_EPOCH_UNIX

    def add_log(self, tag: int, data: bytes) -> None:
        self.state.log.insert(0, (tag, int(self.clock()) - p.LOG_EPOCH_UNIX, data.hex()))
        del self.state.log[LOG_CAPACITY:]

    # --- door mechanics ---------------------------------------------------------

    @property
    def moving(self) -> bool:
        return self.direction != 0

    def channel(self, channel: int) -> None:
        self.state.counters[15] = self.state.counters.get(15, 0) + 1  # command counter
        if channel == 1:
            self.impulse()
        elif channel == 2:
            self.state.light = not self.state.light
            log.info("Light %s", "on" if self.state.light else "off")
            self.changed()
        elif channel == 3:
            self.move_to(POSITION_PARTIAL)
        elif channel == 4:
            self.move_to(POSITION_MAX)
        elif channel == 5:
            self.move_to(0)
        elif channel == 6:
            self.move_to(POSITION_VENTILATION)

    def impulse(self) -> None:
        """Factory-default impulse sequence: open -> stop -> close -> stop -> open ..."""
        if self.moving:
            self.stop()
        elif self.state.position >= POSITION_MAX:
            self.move_to(0)
        elif self.state.position <= 0:
            self.move_to(POSITION_MAX)
        else:
            self.move_to(0 if self.last_direction > 0 else POSITION_MAX)

    def move_to(self, target: int) -> None:
        if target == self.state.position and not self.moving:
            return
        self.target = target
        self.direction = 1 if target > self._exact_position else -1
        self.last_direction = self.direction
        self._run_started_at = self.state.position
        log.info("Door %s towards %d%%", "opening" if self.direction > 0 else "closing", target // 2)
        self.changed()

    def stop(self) -> None:
        if not self.moving:
            return
        self.direction = 0
        self.target = None
        self.state.counters[8] = self.state.counters.get(8, 0) + 1  # incomplete cycles
        self.state.counters[5] = self.state.counters.get(5, 0) + 1
        log.info("Door stopped at %.1f%%", self.state.position / 2)
        self.changed()

    def tick(self, dt: float) -> None:
        if not self.moving or self.target is None:
            return
        step = POSITION_MAX / self.travel_time * dt * self.direction
        self._exact_position += step
        reached = (self.direction > 0 and self._exact_position >= self.target) or (
            self.direction < 0 and self._exact_position <= self.target
        )
        if reached:
            self._exact_position = float(self.target)
        new_position = round(self._exact_position)
        position_changed = new_position != self.state.position
        self.state.position = new_position
        if reached:
            self.direction = 0
            if self.target == 0 and self._run_started_at == POSITION_MAX:
                self.state.counters[7] = self.state.counters.get(7, 0) + 1  # complete cycles
                self.state.counters[4] = self.state.counters.get(4, 0) + 1
            self.target = None
            log.info("Door reached %.1f%%", self.state.position / 2)
            self.changed()
        elif position_changed:
            self.on_change()

    # --- advertisement ----------------------------------------------------------

    def manufacturer_packets(self) -> tuple[bytes, bytes]:
        """The two manufacturer-data payloads (company id 1972) a real drive
        alternates between: a 6-byte status packet and a 17-byte packet with
        position, maintenance flag and serial. Byte layout from
        hoermoles_ble.advertisement; bits are 1-indexed from the LSB there."""
        s = self.state
        status = 1 << 0  # bit 1: BlueSecur
        if s.clock_offset is not None:
            status |= 1 << 2  # bit 3: clock set
        if self.admin_teached:
            status |= 1 << 3  # bit 4: admin taught
        if s.teachable:
            status |= 1 << 5  # bit 6: admins can be taught
        if self.moving:
            status |= 1 << 6  # bit 7: in action
        flags = 0
        if s.low_battery:
            flags |= 1 << 2  # bit 3: low battery
        # bit 7 clear = "teached control" true, bit 8 vacation mode false
        product_data = bytes([status, flags, 0, 0, 0, s.position, 1 if s.maintenance_required else 0]) + bytes(6)
        combined = bytes([s.product_id, s.product_class]) + product_data + struct.pack("<Q", s.serial_no)
        return combined[:6], combined[6:]

    # --- persistence --------------------------------------------------------------

    def changed(self) -> None:
        self.save()
        self.on_change()

    def save(self) -> None:
        if self.state_path is not None:
            self.state.save(self.state_path)

    def describe(self) -> str:
        s = self.state
        motion = {1: "opening", -1: "closing", 0: "stopped"}[self.direction]
        admins = ", ".join(f"#{a.root_id} {a.username}" for a in s.admins.values()) or "none"
        return (
            f"position {s.position / 2:.1f}% ({motion}), light {'on' if s.light else 'off'}, "
            f"admins: {admins}, teach mode: {'OPEN' if s.teachable else 'closed'}"
        )


class DriveSession:
    """One BLE connection: its challenge, its half-received write and its
    registration in progress."""

    def __init__(self, drive: SimulatedDrive, send: Callable[[bytes], None], name: str = "central"):
        self.drive = drive
        self.name = name
        self._send = send
        self._assembler = p.FrameAssembler()
        self._first_chunk_at = 0.0
        self._challenge = secrets.token_bytes(p.SIZE_CHALLENGE)
        self._register_key: bytes | None = None

    def start(self) -> None:
        """Called once the central subscribed to RX: hand out the first challenge."""
        self._notify(p.NOTIF_ENABLED)

    def receive(self, data: bytes) -> None:
        now = time.monotonic()
        if self._assembler.pending and now - self._first_chunk_at > FRAME_TIMEOUT:
            log.warning("[%s] Discarding an incomplete frame older than %.1fs", self.name, FRAME_TIMEOUT)
            self._assembler.reset()
        if not self._assembler.pending:
            self._first_chunk_at = now
        for io_id, body in self._assembler.feed(data):
            if io_id == p.IO_ENCRYPTED:
                self._handle_encrypted(body)
            elif io_id == p.IO_SIGNED:
                self._handle_signed(body)
            else:
                log.warning("[%s] Ignoring io_id=%d frame: %s", self.name, io_id, body.hex())

    # --- outgoing ---------------------------------------------------------------

    def _write(self, frame: bytes) -> None:
        for part in p.chunks(frame):
            self._send(part)

    def _new_challenge(self) -> bytes:
        self._challenge = secrets.token_bytes(p.SIZE_CHALLENGE)
        return self._challenge

    def _notify(self, notif_type: int, payload: bytes = b"") -> None:
        self._write(p.signed_notification(self._new_challenge(), notif_type, payload))

    def _notify_burst(self, list_type: int, end_type: int, payloads: list[bytes]) -> None:
        """A multi-notification answer: every chunk carries the same fresh
        challenge, the last one is the *_END type (with data, or empty)."""
        challenge = self._new_challenge()
        payloads = payloads or [b""]
        for i, payload in enumerate(payloads):
            notif_type = end_type if i == len(payloads) - 1 else list_type
            self._write(p.signed_notification(challenge, notif_type, payload))

    # --- registration (io_id 2, then REGISTER_ROOT) ------------------------------------

    def _handle_encrypted(self, body: bytes) -> None:
        if not self.drive.state.teachable:
            # ASSUMPTION: silence, which is what the client's RegistrationTimeout
            # describes for a drive that is not in teach mode.
            log.warning("[%s] SET_REGISTER_KEY ignored: not in teach mode (reset via menu 19/02)", self.name)
            return
        register_key = self.drive.decrypt_register_key(body)
        if register_key is None:
            log.warning("[%s] SET_REGISTER_KEY could not be decrypted - QR code of another drive?", self.name)
            self._write(p.encrypted_ack(p.ENCRYPTED_ACK_ERROR))
            return
        self._register_key = register_key
        log.info("[%s] SET_REGISTER_KEY accepted, waiting for REGISTER_ROOT", self.name)
        self._write(p.encrypted_ack(p.ENCRYPTED_ACK_COMPLETE))

    def _register_root(self, cmd: p.SignedCommand) -> None:
        if self._register_key is None:
            log.warning("[%s] REGISTER_ROOT without a preceding SET_REGISTER_KEY - ignored", self.name)
            return
        if not cmd.verify(self._register_key, self._challenge):
            log.warning("[%s] REGISTER_ROOT signature invalid - ignored", self.name)
            return
        username = cmd.payload[1:17].rstrip(b"\x00").decode("utf-8", "replace")
        admin, wire = self.drive.add_admin(self._register_key, username)
        self._register_key = None
        log.warning("[%s] Registered admin #%d (%r) - teach mode closed", self.name, admin.root_id, username)
        self._notify(p.NOTIF_ROOT_KEY, struct.pack("<H", admin.root_id) + wire)

    # --- everyday commands (io_id 1) ---------------------------------------------------

    def _handle_signed(self, body: bytes) -> None:
        try:
            cmd = p.SignedCommand.parse(body)
        except ValueError as exc:
            log.warning("[%s] Malformed signed frame: %s", self.name, exc)
            return
        if cmd.command == p.CMD_REGISTER_ROOT:
            self._register_root(cmd)
            return

        admin = self.drive.state.admins.get(cmd.root_id)
        if admin is None:
            log.warning("[%s] %s from unknown root id %d - ignored", self.name, cmd.name, cmd.root_id)
            return
        if not cmd.verify(admin.root_key, self._challenge):
            log.warning("[%s] %s from #%d has an invalid signature - ignored", self.name, cmd.name, cmd.root_id)
            return
        log.info("[%s] %s from admin #%d (%s)", self.name, cmd.name, admin.root_id, admin.username)

        if p.CMD_CHANNEL_FIRST <= cmd.command <= p.CMD_CHANNEL_LAST:
            self._channel(admin, cmd)
        elif cmd.command == p.CMD_GET_PROPERTIES:
            self._send_properties(sorted(self.drive.state.properties.items()))
        elif cmd.command == p.CMD_GET_SELECTED_PROPERTIES:
            self._get_selected_properties(cmd.payload)
        elif cmd.command == p.CMD_SET_PROPERTIES:
            self._set_properties(cmd.payload)
        elif cmd.command == p.CMD_GET_LOG:
            payloads = [p.log_entry_payload(tag, ts, bytes.fromhex(data)) for tag, ts, data in self.drive.state.log]
            self._notify_burst(p.NOTIF_LOG, p.NOTIF_LOG_END, payloads)
        elif cmd.command == p.CMD_READ_SERVICE_DATA:
            self._send_service_data()
        else:
            log.warning("[%s] Command %s not simulated - no answer", self.name, cmd.name)

    def _channel(self, admin: Admin, cmd: p.SignedCommand) -> None:
        channel = cmd.command - p.CMD_CHANNEL_FIRST + 1
        if len(cmd.payload) >= 8:
            sent_at = struct.unpack_from("<Q", cmd.payload)[0]
            skew = sent_at - time.time()
            if abs(skew) > 60:
                log.warning("[%s] Command timestamp is %+.0fs off the host clock", self.name, skew)
            old, new = self.drive.set_clock(sent_at)
            self.drive.add_log(p.LOG_IMPULS_WITH_CLOCK, struct.pack("<HBII", admin.root_id, channel, old, new))
        log.info("[%s] Channel %d (%s)", self.name, channel, CHANNEL_NAMES[channel])
        self.drive.channel(channel)
        # The challenge is spent. ASSUMPTION: no answer - the real drive drops the
        # link ~100-150 ms after a command, which a macOS peripheral cannot do,
        # so a second command on this connection fails its signature check.
        self._new_challenge()
        log.info("[%s] (a real drive would disconnect now)", self.name)

    def _send_properties(self, items: list[tuple[int, int]]) -> None:
        n = PROPERTIES_PER_NOTIFICATION
        payloads = [p.properties_payload(items[i : i + n]) for i in range(0, len(items), n)]
        self._notify_burst(p.NOTIF_PROPERTIES_LIST, p.NOTIF_PROPERTIES_LIST_END, payloads)

    def _get_selected_properties(self, payload: bytes) -> None:
        groups = [g for g in payload if g != 0xFF]
        if any(g < 100 for g in groups) and any(g >= 100 for g in groups):
            # Live-observed: the real drive drops the connection on such a request.
            log.warning("[%s] GET_SELECTED_PROPERTIES mixes groups <100 and >=100 - real drive disconnects", self.name)
            return
        props = self.drive.state.properties
        self._send_properties([(g, props[g]) for g in groups if g in props])

    def _set_properties(self, payload: bytes) -> None:
        count = payload[0] if payload else 0
        items = [
            (payload[1 + i * 3], struct.unpack_from("<h", payload, 2 + i * 3)[0])
            for i in range(count)
            if 1 + i * 3 + 3 <= len(payload)
        ]
        valid = _valid_property_values()
        for group, value in items:
            if group not in valid or (valid[group] is not None and value not in valid[group]):
                log.warning("[%s] SET_PROPERTIES rejected: menu group %d = %d", self.name, group, value)
                self._notify(p.NOTIF_PROPERTIES_INVALID)
                return
        self.drive.state.properties.update(items)
        self.drive.save()
        log.info("[%s] SET_PROPERTIES %s", self.name, dict(items))
        self._notify(p.NOTIF_PROPERTY_ACCEPTED)

    def _send_service_data(self) -> None:
        counters = self.drive.state.counters
        items = sorted(counters.items())
        n = SERVICE_DATA_PER_NOTIFICATION
        payloads = [p.service_data_payload(items[i : i + n]) for i in range(0, len(items), n)]
        self._notify_burst(p.NOTIF_SERVICE_DATA, p.NOTIF_SERVICE_DATA_END, payloads)
