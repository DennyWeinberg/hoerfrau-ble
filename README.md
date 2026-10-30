# Hörfrau

A simulated Hörmann SupraMatic garage door drive with BlueSecur Bluetooth. It runs on a Mac and
behaves over BLE like the real drive, so the pairing and control code in
[hoermoles-ble](https://github.com/the78mole/hoermoles-ble) and
[hoermoles-ble-homeassistant](https://github.com/the78mole/hoermoles-ble-homeassistant) can be
developed without standing in the garage.

What it simulates:

- **QR label** ("barcode" on the drive): `03 02 02 000 <20-digit serial> 00` + base64 RSA public key,
  printed as text and as a terminal QR code you can scan with a phone.
- **Teach mode / admin reset** (menu 19 / parameter 02): only then is a new registration accepted;
  a reset deletes all admins.
- **Registration**: RSA-encrypted register key → ack → HMAC-signed `REGISTER_ROOT` → `ROOT_KEY`
  (root id + root key XOR register key).
- **Signed commands** with per-connection challenges: channels 1-6 (impulse, light, partial, open,
  close, ventilation), menu properties get/set, audit log, service counters.
- **Door mechanics**: position, travel time, impulse sequence open → stop → close → stop.
- **Advertisement bytes** (manufacturer data 1972, short + long packet). macOS can't send them over
  the air; see [Home Assistant](#home-assistant-debug-matcher).

State (serial, RSA key, admins, door position, log) persists in `~/.hoerfrau/drive.json`
(override with `--state` or `HOERFRAU_STATE`), so a pairing survives a simulator restart.

## Usage

```bash
uv sync
uv run hoerfrau run            # start the drive (BLE peripheral), prints the QR label
uv run hoerfrau label --svg label.svg
uv run hoerfrau status         # state + advertisement bytes
uv run hoerfrau reset-admin    # menu 19/02 while the simulator is not running
uv run hoerfrau new --force    # throw the drive away, new serial and key
uv run hoerfrau -v run         # log every BLE chunk
```

While `run` is active, type into its console what you'd do at the real drive:
`reset` (menu 19/02), `impulse`, `open`, `close`, `stop`, `partial`, `vent`, `light`,
`battery`, `maintenance`, `label`, `adv`, `status`, `quit`.

The first `run` asks for Bluetooth permission for your terminal or IDE (System Settings → Privacy &
Security → Bluetooth). A Mac cannot connect to its own peripheral, so test it from another machine
(the Home Assistant box, or a phone with nRF Connect).

Typical pairing test: `run`, then `reset` if teach mode is closed, then paste the printed label
text into the client under test.

## Home Assistant (debug matcher)

macOS only lets a peripheral advertise a local name and service UUIDs, so the real drive's
manufacturer data (company id 1972), which the integration's manifest matches and parses, is
**not** on the air. To make HA see the simulator, a debug matcher is needed in the integration:

| What | Value |
|---|---|
| Local name | `HFSIM` + last 4 serial digits, e.g. `HFSIM1030` |
| Manifest matcher | `{"local_name": "HFSIM*"}` (next to `{"manufacturer_id": 1972}`) |
| Advertised service | `669a9001-0008-968f-e311-6050405558b3` (the real BlueConnect service) |
| GATT | real BlueConnect TX `669a900c-…` (write) / RX `669a900a-…` (notify) |
| Debug service | `686f6572-6672-6175-0001-000000000000` (simulator only) |
| Debug characteristic | `686f6572-6672-6175-0002-000000000000`, read + notify |

The debug characteristic returns the advertisement the real drive would send, as JSON:

```json
{"company_id":1972,"packets":["020221000000","00000000000000000016286c40ad836b00"],"serial_no":30262602641451030}
```

Feeding both `packets` into `AdvertisementInfo.from_scan()` gives the same result as a real scan.
`config_flow.py` filters discovered devices on `MANUFACTURER_ID`, so the debug path has to
accept the `HFSIM` name there as well.

**Use `--name HFSIM` for Home Assistant** (observed 2026-10-30, macOS + HAOS with an Intel AX201):
the advertisement is 31 bytes, and macOS fills 24 of them with flags (3), TX power (3) and the
128-bit service UUID (18). The default name `HFSIM` + 4 digits needs 11 more, and macOS then
drops it, also from the scan response, so HA only sees an unnamed device with the BlueConnect
UUID. Bare `HFSIM` (7 bytes) fits exactly:

```bash
uv run hoerfrau run --name HFSIM
```

On the Home Assistant side, also:

- Set the Bluetooth adapter to **active** scanning (Settings → Devices & services → Bluetooth →
  *Configure*); in `auto` mode the name was not picked up.
- BlueZ tries a Bluetooth Classic connection to the Mac (`br-connection-unknown`), because macOS
  advertises from its public address with flags that say "BR/EDR capable". The integration sets
  BlueZ's `PreferredBearer` to `le` for `HFSIM` devices before connecting.

Differences from the real drive to keep in mind:

- **No disconnect after a command.** The real drive drops the link ~100-150 ms after a command; a
  macOS peripheral can't disconnect a central. The challenge is spent anyway, so a second command on
  the same connection fails its signature check, as it would on the real drive after reconnecting.
- **Address**: HA sees the Mac's BLE address, which macOS may rotate. Config entries are keyed by
  address. (Observed 2026-10-30: the Mac advertised from its public Bluetooth address, which
  doesn't rotate.)
- **Refused registration is silent**: outside teach mode the drive doesn't answer, and
  `HoermannClient.register()` then waits a hardcoded 20 s (its `timeout` argument doesn't cover
  that step).
- Choices marked `ASSUMPTION` in [drive.py](src/hoerfrau/drive.py) are not confirmed against
  hardware (e.g. teach mode closes after one registration, no answer to channel commands).

## In-process use (no radio)

`LoopbackTransport` plugs `hoermoles_ble.HoermannClient` straight into the simulator, for tests:

```python
from hoermoles_ble import HoermannClient
from hoerfrau.drive import DriveState, SimulatedDrive
from hoerfrau.loopback import LoopbackTransport

drive = SimulatedDrive(DriveState.create(key_size=1024))
async with HoermannClient(LoopbackTransport(drive)) as client:
    await client.wait_for_any_notification()
    creds = await client.register(drive.qr_text, "AA:BB:CC:DD:EE:FF")
    await client.open_channel(creds, channel=1)
```

## Development

```bash
uv run pytest
```

The tests run the real `hoermoles-ble` client against the simulator. The simulator's protocol code
([protocol.py](src/hoerfrau/protocol.py)) is written separately from the library, so a bug in one
can't hide the same bug in the other.

Layout: `protocol.py` (framing, signatures), `drive.py` (state, door, sessions), `macos.py`
(CoreBluetooth peripheral), `loopback.py` (in-process transport), `cli.py`.
