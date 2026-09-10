"""Exercise real protocol and application failure paths without radio hardware."""
import asyncio
import json
import struct
import zipfile
from types import SimpleNamespace

import pytest

import akita_zmodem_meshcore as azm
import zmodem as zm
from mesh_transport import decode_mesh_message


def start_frame(name='x', size=4):
    name = name.encode('utf-8')
    return zm._frame(b'S' + struct.pack('!H', len(name)) + name + struct.pack('!Q', size))


def make_app(tmp_path, **overrides):
    config = {'tx_delay_ms': 0, 'allow_unsafe_tx_delay': True,
              'retransmit_timeout_s': 0.02, 'incoming_dir': str(tmp_path / 'inbox')}
    config.update(overrides)
    app = azm.AkitaZmodemMeshCore(config)

    class Mesh:
        commands = None

        async def send_msg(self, dest, message):
            pass

    app.mesh = Mesh()
    app.mesh.commands = app.mesh
    return app


@pytest.mark.asyncio
async def test_fragmented_start_preserves_name_and_checks_size(tmp_path):
    app = make_app(tmp_path)
    name = 'long-' + 'x' * 180 + '.bin'
    frame = start_frame(name)
    for pos in range(0, len(frame), 16):
        await app._handle_zmodem_data('peer', frame[pos:pos + 16])
    assert (tmp_path / 'inbox' / name).exists()
    await app.stop()

    app = make_app(tmp_path, max_file_size_bytes=3)
    dest = tmp_path / 'explicit.bin'
    tid = await app.receive_file(str(dest))
    for pos in range(0, len(frame), 16):
        await app._handle_zmodem_data('peer', frame[pos:pos + 16])
    assert tid not in app.transfers
    assert not dest.exists()
    await app.stop()


@pytest.mark.asyncio
async def test_bad_start_crc_does_not_create_file(tmp_path):
    app = make_app(tmp_path)
    frame = start_frame()
    await app._handle_zmodem_data('peer', frame[:-1] + bytes([frame[-1] ^ 1]))
    assert not app.transfers
    assert not (tmp_path / 'inbox').exists()


@pytest.mark.asyncio
async def test_waiting_receiver_does_not_intercept_active_peer(tmp_path):
    app = make_app(tmp_path)
    waiting = await app.receive_file(str(tmp_path / 'waiting'))
    active = 99
    app.transfers[active] = {'state': 'sending', 'dest': 'peer'}
    assert await app._match_transfer('peer', zm._frame(b'A' + struct.pack('!Q', 0))) == active
    assert app.transfers[waiting]['state'] == 'waiting'
    await app.stop()


@pytest.mark.asyncio
async def test_retransmission_does_not_reset_idle_timeout(tmp_path):
    app = make_app(tmp_path)
    src = tmp_path / 'source'
    src.write_bytes(b'data')
    tid = await app.send_file('peer', str(src))
    original = app.transfers[tid]['last_act']
    await asyncio.sleep(0.25)
    assert app.transfers[tid]['last_act'] == original
    await app.stop()
    await asyncio.sleep(0.15)


@pytest.mark.asyncio
async def test_transfer_recovers_send_failure_loss_and_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(azm, 'TQDM_AVAILABLE', False)
    sender_app = make_app(tmp_path / 'sender')
    receiver_app = make_app(tmp_path / 'receiver')
    faults = set()
    data_fragments = 0

    class Link:
        def __init__(self, other, source):
            self.other = other
            self.source = source
            self.commands = self

        async def send_msg(self, dest, message):
            nonlocal data_fragments
            raw = decode_mesh_message(message)[2:]
            if self.source == 'sender':
                if 'send_error' not in faults:
                    faults.add('send_error')
                    raise OSError('simulated first-send failure')
                if raw[4:5] == b'D':
                    data_fragments = 1
                elif data_fragments:
                    data_fragments += 1
                if data_fragments == 2 and 'lost_fragment' not in faults:
                    faults.add('lost_fragment')
                    return
            elif raw == zm._frame(b'E') and 'lost_end' not in faults:
                faults.add('lost_end')
                return
            event = SimpleNamespace(payload={'pubkey_prefix': self.source, 'text': message})
            await self.other._on_mesh_message(event)
            if self.source == 'receiver' and raw[4:5] == b'A' and 'duplicate_ack' not in faults:
                faults.add('duplicate_ack')
                await self.other._on_mesh_message(event)

    sender_app.mesh = Link(receiver_app, 'sender')
    receiver_app.mesh = Link(sender_app, 'receiver')
    source = tmp_path / 'source.bin'
    source.write_bytes(bytes(range(256)) * 5)
    target = tmp_path / 'received.bin'
    sent, received = asyncio.Event(), asyncio.Event()
    listeners = [asyncio.create_task(app._receive_loop_processor())
                 for app in (sender_app, receiver_app)]
    try:
        await receiver_app.receive_file(str(target), cli_event=received)
        await sender_app.send_file('receiver', str(source), sent)
        await asyncio.wait_for(asyncio.gather(sent.wait(), received.wait()), 6)
        assert target.read_bytes() == source.read_bytes()
        assert faults == {'send_error', 'lost_fragment', 'duplicate_ack', 'lost_end'}
        assert not sender_app.transfers
        assert not receiver_app.transfers
    finally:
        await sender_app.stop()
        await receiver_app.stop()
        for task in listeners:
            task.cancel()
        await asyncio.gather(*listeners, return_exceptions=True)


@pytest.mark.parametrize('value', [None, True, float('nan'), float('inf')])
def test_invalid_numeric_configuration(value, tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'timeout': value}))
    with pytest.raises(ValueError):
        azm.AkitaZmodemMeshCore(config_file=str(config))


@pytest.mark.parametrize('value', [[], None, 'text'])
def test_configuration_requires_object(value, tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='JSON object'):
        azm.load_config(str(config))


@pytest.mark.parametrize('directory_link', [False, True])
def test_zip_rejects_existing_symlink_escape(tmp_path, directory_link):
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'file'
    target.write_text('original')
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'link').symlink_to(outside if directory_link else target)
    archive = tmp_path / 'test.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('link/file' if directory_link else 'link', 'replacement')
    with pytest.raises(azm.UnsafeZipError):
        azm._safe_extract_zip(archive, output)
    assert target.read_text() == 'original'


def test_zip_preserves_empty_directories(tmp_path):
    archive = tmp_path / 'test.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('empty/nested/', '')
    azm._safe_extract_zip(archive, tmp_path / 'output')
    assert (tmp_path / 'output/empty/nested').is_dir()


@pytest.mark.asyncio
async def test_cancel_stops_sending_fragments(tmp_path):
    app = make_app(tmp_path)
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_send(dest, message):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    app.mesh.send_msg = blocked_send
    tid = await app.send_file('peer', str(source))
    await asyncio.wait_for(entered.wait(), 1)
    app.cancel_transfer(tid)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert app._transfer_results[tid] is False
    await app.stop()


@pytest.mark.asyncio
async def test_cancelled_directory_is_not_extracted(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    archive_paths = []

    async def cancelled_receive(filepath, overwrite, event):
        archive_paths.append(filepath)
        with zipfile.ZipFile(filepath, 'w') as z:
            z.writestr('should-not-exist', 'data')
        app._transfer_results[1] = False
        event.set()
        return 1

    monkeypatch.setattr(app, 'receive_file', cancelled_receive)
    target = tmp_path / 'output'
    event = asyncio.Event()
    await app.receive_directory(str(target), False, event)
    assert event.is_set()
    assert list(target.iterdir()) == []
    from pathlib import Path
    assert not Path(archive_paths[0]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['send', 'status'])
async def test_cli_returns_failure_status(tmp_path, monkeypatch, command):
    import sys
    if command == 'send':
        async def cannot_connect(self):
            return False
        monkeypatch.setattr(azm.AkitaZmodemMeshCore, '_connect_mesh', cannot_connect)
        args = ['send', 'peer', str(tmp_path / 'missing')]
    else:
        async def failed_status(*args):
            return {'ok': False, 'error': 'not running'}
        monkeypatch.setattr(azm, 'send_control_command', failed_status)
        args = ['status']
    monkeypatch.setattr(sys, 'argv', ['akita'] + args)
    assert await azm.main() == 1
