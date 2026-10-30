"""The real hoermoles_ble client against the simulator, over the loopback transport."""

from __future__ import annotations

import asyncio

import pytest
from hoermoles_ble import AdvertisementInfo, HoermannClient, RegistrationTimeout
from hoermoles_ble.credentials import Credentials
from hoermoles_ble.device_log import LOG_TAG_NAMES, parse_log_fields
from hoermoles_ble.protocol import parse_qr_code, product_class_and_id_from_qr_prefix, serial_no_from_qr_prefix

from hoerfrau.drive import POSITION_MAX, DriveState, SimulatedDrive
from hoerfrau.loopback import LoopbackTransport

ADDRESS = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(scope="module")
def state_template() -> DriveState:
    # RSA key generation is the slow part - do it once per module.
    return DriveState.create(serial_no=30262602641451030, key_size=1024)


@pytest.fixture
def drive(state_template, tmp_path) -> SimulatedDrive:
    state = DriveState.from_json(state_template.to_json())
    return SimulatedDrive(state, state_path=tmp_path / "drive.json", travel_time=1.0)


async def register(drive: SimulatedDrive) -> Credentials:
    async with HoermannClient(LoopbackTransport(drive)) as client:
        await client.wait_for_any_notification(timeout=1)
        return await client.register(drive.qr_text, ADDRESS, timeout=1)


async def send_channel(drive: SimulatedDrive, creds: Credentials, channel: int) -> None:
    async with HoermannClient(LoopbackTransport(drive)) as client:
        await client.wait_for_any_notification(timeout=1)
        await client.open_channel(creds, channel=channel)


def test_qr_label_parses_like_a_real_one(drive):
    prefix, der = parse_qr_code(drive.qr_text)
    assert len(prefix) == 31
    assert serial_no_from_qr_prefix(prefix) == drive.state.serial_no
    assert product_class_and_id_from_qr_prefix(prefix) == (2, 2)
    assert der.startswith(b"\x30")  # DER SEQUENCE


def test_advertisement_parses(drive):
    short, long = drive.manufacturer_packets()
    assert (len(short), len(long)) == (6, 17)

    info = AdvertisementInfo.from_scan(ADDRESS, -50, [long, short])
    assert info.parse_error is None
    assert info.product_name == "Supramatic Serie 4"
    assert info.serial_no == drive.state.serial_no
    assert info.admins_can_be_teached is True
    assert info.admin_teached is False
    assert info.opening_progress_percent == 0

    lone = AdvertisementInfo.from_scan(ADDRESS, -50, [long], product_class=2)
    assert lone.serial_no == drive.state.serial_no


async def test_register_then_control(drive):
    creds = await register(drive)
    assert creds.root_id == 1
    assert creds.root_key == drive.state.admins[1].root_key
    assert drive.state.admins[1].username == "ArnoNym"
    assert drive.state.teachable is False

    info = AdvertisementInfo.from_scan(ADDRESS, -50, list(drive.manufacturer_packets()))
    assert (info.admin_teached, info.admins_can_be_teached) == (True, False)

    await send_channel(drive, creds, channel=1)
    assert drive.moving and drive.direction == 1
    drive.tick(2.0)
    assert drive.state.position == POSITION_MAX and not drive.moving


async def test_registration_refused_outside_teach_mode(drive):
    await register(drive)
    # The drive stays silent; register() waits a hardcoded 20s for the ack
    # (its timeout argument only covers the later ROOT_KEY step), so cap it here.
    with pytest.raises(TimeoutError):
        async with HoermannClient(LoopbackTransport(drive)) as client:
            await client.wait_for_any_notification(timeout=1)
            async with asyncio.timeout(0.3):
                await client.register(drive.qr_text, ADDRESS)
    assert list(drive.state.admins) == [1]


async def test_reset_reopens_registration_and_revokes_old_key(drive):
    old = await register(drive)
    drive.reset_admins()
    new = await register(drive)
    assert new.root_id != old.root_id

    await send_channel(drive, old, channel=1)
    assert not drive.moving
    await send_channel(drive, new, channel=1)
    assert drive.moving


async def test_wrong_key_is_ignored(drive):
    creds = await register(drive)
    forged = Credentials(device_address=ADDRESS, root_id=creds.root_id, root_key=bytes(32))
    await send_channel(drive, forged, channel=1)
    assert not drive.moving


async def test_qr_of_another_drive_fails_registration(drive, state_template):
    other = SimulatedDrive(DriveState.create(serial_no=1, key_size=1024))
    with pytest.raises(RegistrationTimeout):
        async with HoermannClient(LoopbackTransport(drive)) as client:
            await client.wait_for_any_notification(timeout=1)
            await client.register(other.qr_text, ADDRESS, timeout=1)


async def test_impulse_sequence(drive):
    creds = await register(drive)
    await send_channel(drive, creds, 1)  # open
    drive.tick(0.5)
    await send_channel(drive, creds, 1)  # stop
    assert not drive.moving and 0 < drive.state.position < POSITION_MAX
    await send_channel(drive, creds, 1)  # close
    assert drive.direction == -1


async def test_properties_log_and_service_data(drive):
    creds = await register(drive)
    await send_channel(drive, creds, 1)

    async with HoermannClient(LoopbackTransport(drive)) as client:
        await client.wait_for_any_notification(timeout=1)
        props = await client.read_properties(creds, timeout=1)
        assert props == drive.state.properties

        group = next(iter(props))
        selected = await client.read_properties(creds, menu_groups=[group], timeout=1)
        assert selected == {group: props[group]}

        entries = await client.read_log(creds, timeout=1)
        names = [LOG_TAG_NAMES[tag] for tag, _ts, _data in entries]
        assert names == ["IMPULS_WITH_CLOCK", "REGISTER_ROOT"]
        assert parse_log_fields(entries[0][0], entries[0][2])["toggled_channel"] == 1

        counters = await client.read_service_data(creds, timeout=1)
        assert counters[15] == 1  # command counter


async def test_set_properties(drive):
    from hoermoles_ble import PropertiesRejected
    from hoermoles_ble.menu_settings import menu_table_for_product

    setting = next(s for s in menu_table_for_product(2, 2).settings if len(s.parameters) >= 2)
    value = setting.parameters[-1].value
    creds = await register(drive)
    async with HoermannClient(LoopbackTransport(drive)) as client:
        await client.wait_for_any_notification(timeout=1)
        await client.write_properties(creds, {setting.menu_group: value}, timeout=1)
        assert drive.state.properties[setting.menu_group] == value
        with pytest.raises(PropertiesRejected):
            await client.write_properties(creds, {setting.menu_group: 12345}, timeout=1)


async def test_state_survives_restart(drive):
    creds = await register(drive)
    reloaded = SimulatedDrive(DriveState.load_or_create(drive.state_path))
    await send_channel(reloaded, creds, 1)
    assert reloaded.moving


async def test_slow_frame_is_discarded(drive, monkeypatch):
    import hoerfrau.drive as drive_module

    creds = await register(drive)
    monkeypatch.setattr(drive_module, "FRAME_TIMEOUT", 0.05)
    async with HoermannClient(LoopbackTransport(drive)) as client:
        await client.wait_for_any_notification(timeout=1)
        original = client._transport.write

        async def slow_write(data):
            await original(data)
            await asyncio.sleep(0.1)

        client._transport.write = slow_write
        await client.open_channel(creds, channel=1)
    assert not drive.moving
