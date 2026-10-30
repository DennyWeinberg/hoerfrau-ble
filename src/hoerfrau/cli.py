"""Command line: run the simulated drive, print its QR label, reset its admin."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import qrcode
import qrcode.image.svg

from .drive import CHANNEL_NAMES, DriveState, SimulatedDrive

DEFAULT_STATE = Path(os.environ.get("HOERFRAU_STATE", "~/.hoerfrau/drive.json")).expanduser()
TICK = 0.1

CONSOLE_HELP = """\
Drive console - what you'd do standing at the real drive:
  reset         menu 19 / parameter 02: delete all admins, accept a new registration
  impulse       press the impulse button (open/stop/close/stop ...)
  open | close | stop | partial | vent | light
  battery       toggle the low-battery flag
  maintenance   toggle the maintenance-due flag
  label         show the QR label again
  adv           show the (would-be) manufacturer advertisement bytes
  status        show the drive state
  quit"""


def _load(args) -> SimulatedDrive:
    state = DriveState.load_or_create(args.state)
    return SimulatedDrive(state, state_path=args.state, travel_time=getattr(args, "travel_time", 15.0))


def local_name_for(drive: SimulatedDrive) -> str:
    # Must fit into the 31-byte advertisement next to what macOS puts there:
    # flags (3), TX power (3) and the 128-bit service UUID (18) leave 7 bytes,
    # i.e. a 5-character name. A longer one is dropped altogether, also from the
    # scan response, so Home Assistant would only see an unnamed device.
    return "HFSIM"


def print_label(drive: SimulatedDrive, svg: Path | None = None) -> None:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
    qr.add_data(drive.qr_text)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    print(f"\nQR label content (serial {drive.state.serial_no}) - paste this where the barcode would be scanned:\n")
    print(drive.qr_text)
    print()
    if svg is not None:
        qrcode.make(drive.qr_text, image_factory=qrcode.image.svg.SvgPathImage).save(str(svg))
        print(f"QR label also written to {svg}")


def print_adv(drive: SimulatedDrive) -> None:
    short, long = drive.manufacturer_packets()
    print(f"manufacturer data 1972, short packet: {short.hex()}")
    print(f"manufacturer data 1972, long packet:  {long.hex()}")


# --- commands -------------------------------------------------------------------------


def cmd_new(args) -> None:
    if args.state.exists() and not args.force:
        sys.exit(f"{args.state} exists - pass --force to replace this drive with a new one")
    state = DriveState.create(serial_no=args.serial, key_size=args.key_size)
    state.save(args.state)
    print(f"New drive with serial {state.serial_no} saved to {args.state}")
    print_label(SimulatedDrive(state))


def cmd_label(args) -> None:
    print_label(_load(args), args.svg)


def cmd_status(args) -> None:
    drive = _load(args)
    print(f"State file: {args.state}")
    print(f"Advertised name: {local_name_for(drive)}")
    print(drive.describe())
    print_adv(drive)


def cmd_reset_admin(args) -> None:
    drive = _load(args)
    drive.reset_admins()


def cmd_run(args) -> None:
    if sys.platform != "darwin":
        sys.exit("The BLE peripheral is implemented for macOS only.")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


async def _run(args) -> None:
    from .macos import DEBUG_ADV, MacPeripheral

    drive = _load(args)
    name = args.name or local_name_for(drive)
    peripheral = MacPeripheral(drive, name)

    def on_change() -> None:
        peripheral.refresh_debug_value()

    drive.on_change = on_change
    await peripheral.start()

    print_label(drive)
    logging.getLogger("hoerfrau").info(
        "Advertising as %r with the BlueConnect service - debug characteristic %s. %s",
        name,
        DEBUG_ADV,
        drive.describe(),
    )
    print("Type 'help' for the drive console.\n")

    ticker = asyncio.create_task(_tick(drive))
    try:
        await _console(drive)
    finally:
        ticker.cancel()
        drive.save()
        await peripheral.stop()


async def _tick(drive: SimulatedDrive) -> None:
    while True:
        await asyncio.sleep(TICK)
        drive.tick(TICK)


async def _console(drive: SimulatedDrive) -> None:
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    def on_stdin() -> None:
        line = sys.stdin.readline()
        lines.put_nowait(line if line else None)

    loop.add_reader(sys.stdin, on_stdin)
    channels = {name: number for number, name in CHANNEL_NAMES.items()} | {"vent": 6}
    try:
        while True:
            line = await lines.get()
            if line is None:  # stdin closed: keep simulating until Ctrl-C
                loop.remove_reader(sys.stdin)
                await asyncio.Event().wait()
            command = line.strip().lower()
            if not command:
                continue
            if command in ("quit", "exit", "q"):
                return
            if command == "help":
                print(CONSOLE_HELP)
            elif command == "reset":
                drive.reset_admins()
            elif command == "stop":
                drive.stop()
            elif command in channels:
                drive.channel(channels[command])
            elif command == "battery":
                drive.state.low_battery = not drive.state.low_battery
                drive.changed()
                print(f"low battery: {drive.state.low_battery}")
            elif command == "maintenance":
                drive.state.maintenance_required = not drive.state.maintenance_required
                drive.changed()
                print(f"maintenance required: {drive.state.maintenance_required}")
            elif command == "label":
                print_label(drive)
            elif command == "adv":
                print_adv(drive)
            elif command == "status":
                print(drive.describe())
            else:
                print(f"unknown command {command!r} - try 'help'")
    finally:
        loop.remove_reader(sys.stdin)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hoerfrau", description="Simulated Hörmann SupraMatic BLE drive")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help=f"drive state file (default {DEFAULT_STATE})")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every BLE chunk")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the drive as a BLE peripheral (macOS)")
    run.add_argument("--name", help="advertised local name (default HFSIM; longer than 5 characters isn't sent on macOS)")
    run.add_argument("--travel-time", type=float, default=15.0, help="seconds for a full open/close run")
    run.set_defaults(func=cmd_run)

    label = sub.add_parser("label", help="print the QR label (the 'barcode' on the drive)")
    label.add_argument("--svg", type=Path, help="also write the QR code as SVG")
    label.set_defaults(func=cmd_label)

    sub.add_parser("status", help="print the drive state").set_defaults(func=cmd_status)
    sub.add_parser("reset-admin", help="menu 19/02 while the simulator is not running").set_defaults(
        func=cmd_reset_admin
    )

    new = sub.add_parser("new", help="create a new drive (new serial number and RSA key)")
    new.add_argument("--serial", type=int, help="serial number (default random)")
    new.add_argument("--key-size", type=int, default=2048, help="RSA key size in bits")
    new.add_argument("--force", action="store_true", help="replace an existing drive")
    new.set_defaults(func=cmd_new)

    args = parser.parse_args(argv)
    logging.basicConfig(format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S", level=logging.INFO)
    if args.verbose:
        logging.getLogger("hoerfrau").setLevel(logging.DEBUG)
    args.func(args)
