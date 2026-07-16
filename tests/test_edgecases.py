import asyncio

import pytest

from akita_zmodem_meshcore import AkitaZmodemMeshCore, _safe_extract_zip


@pytest.mark.asyncio
async def test_send_file_with_directory(tmp_path):
    app = AkitaZmodemMeshCore()
    app.mesh = object()  # stub so send_file doesn't early exit
    # create a directory
    d = tmp_path / "dir"
    d.mkdir()
    tid = await app.send_file('peer', str(d))
    assert tid is None


def test_validate_config_types(tmp_path):
    # write invalid config
    cfg = tmp_path / "bad.json"
    cfg.write_text('{"mesh_packet_chunk_size": "large"}')
    with pytest.raises(ValueError):
        AkitaZmodemMeshCore(config_file=str(cfg))


@pytest.mark.parametrize(
    "content",
    [
        '{"zmodem_app_port": 70000}',
        '{"chunk_size": 0}',
        '{"mesh_connection_type": "bluetooth"}',
        '{"mesh_tcp_port": 0}',
        '{"mesh_serial_baud": -1}',
        '{"tx_delay_ms": -1}',
    ],
)
def test_validate_config_rejects_invalid_runtime_values(tmp_path, content):
    cfg = tmp_path / "bad_runtime.json"
    cfg.write_text(content)
    with pytest.raises(ValueError):
        AkitaZmodemMeshCore(config_file=str(cfg))


def test_validate_config_rejects_oversized_mesh_chunk(tmp_path):
    cfg = tmp_path / "bad_chunk.json"
    cfg.write_text('{"mesh_packet_chunk_size": 185}')
    with pytest.raises(ValueError):
        AkitaZmodemMeshCore(config_file=str(cfg))


@pytest.mark.parametrize(
    "content",
    [
        '{"mesh_packet_chunk_size": 17}',
        '{"chunk_size": 4097}',
        '{"tx_delay_ms": 0}',
        '{"max_consecutive_send_failures": 0}',
        '{"max_inbound_queue": 0}',
        '{"max_file_size_bytes": -1}',
    ],
)
def test_validate_config_rejects_network_risk_values(tmp_path, content):
    cfg = tmp_path / "bad_network.json"
    cfg.write_text(content)
    with pytest.raises(ValueError):
        AkitaZmodemMeshCore(config_file=str(cfg))


def test_validate_config_allows_explicit_unsafe_tx_delay_override(tmp_path):
    cfg = tmp_path / "lab.json"
    cfg.write_text('{"tx_delay_ms": 0, "allow_unsafe_tx_delay": true}')
    app = AkitaZmodemMeshCore(config_file=str(cfg))
    assert app.tx_delay_s == 0


def test_safe_extract_zip_memory(tmp_path):
    # a large zip with many entries should still be handled; just ensure no
    # crash
    zip_path = tmp_path / "many.zip"
    import zipfile
    with zipfile.ZipFile(str(zip_path), 'w') as z:
        for i in range(1000):
            z.writestr(f'file{i}.txt', 'data')
    out = tmp_path / "out"
    out.mkdir()
    _safe_extract_zip(str(zip_path), str(out))
    assert (out / 'file999.txt').exists()


def test_safe_extract_zip_rejects_existing_file_without_overwrite(tmp_path):
    zip_path = tmp_path / "incoming.zip"
    import zipfile
    with zipfile.ZipFile(str(zip_path), 'w') as z:
        z.writestr('same.txt', 'new')
        z.writestr('other.txt', 'other')

    out = tmp_path / "out"
    out.mkdir()
    existing = out / "same.txt"
    existing.write_text("old")

    with pytest.raises(FileExistsError):
        _safe_extract_zip(str(zip_path), str(out), overwrite=False)

    assert existing.read_text() == "old"
    assert not (out / "other.txt").exists()


@pytest.mark.asyncio
async def test_receive_file_rejects_blocked_parent_path(tmp_path):
    # Simulate a parent path that cannot be created because a file is present.
    app = AkitaZmodemMeshCore()
    app.mesh = object()
    # prepare settings to trigger error in _handle_zmodem_data
    dest = tmp_path / "blocked" / "incoming.bin"
    dest.parent.write_text("not a directory")
    tid = await app.receive_file(str(dest), overwrite=True)
    assert tid is None


@pytest.mark.asyncio
async def test_receive_file_rejects_directory_destination(tmp_path):
    app = AkitaZmodemMeshCore()
    cli_event = asyncio.Event()

    tid = await app.receive_file(
        str(tmp_path),
        overwrite=True,
        cli_event=cli_event,
    )

    assert tid is None
    assert cli_event.is_set()


@pytest.mark.asyncio
async def test_handle_zmodem_data_receiver_init_failure(tmp_path, monkeypatch):
    app = AkitaZmodemMeshCore()
    app.mesh = object()
    dest = tmp_path / "incoming.bin"
    tid = await app.receive_file(str(dest), overwrite=True)
    # craft a fake payload that _handle_zmodem_data will accept
    # monkeypatch zmodem.Receiver to simple object

    class DummyRecv:
        def __init__(self, f):
            raise IsADirectoryError(f"Is a directory: '{f}'")

        def receive(self, data):
            return b''

        def is_finished(self):
            return False

    monkeypatch.setattr('zmodem.Receiver', DummyRecv)
    app._looks_like_zmodem_start = lambda data: True
    # run handler with dummy data
    await app._handle_zmodem_data('peer', b'hello')
    # transfer should have been cancelled
    assert tid not in app.transfers


@pytest.mark.asyncio
async def test_send_directory_skips_symlinks(tmp_path):
    # create directory with real file and symlink
    d = tmp_path / "d"
    d.mkdir()
    real = d / "real.txt"
    real.write_text("data")
    link = d / "link.txt"
    link.symlink_to(real)
    app = AkitaZmodemMeshCore()
    app.mesh = object()
    # monkeypatch send_file to capture path of zip created
    called = {}

    async def fake_send(dest, path, cli_event=None):
        called['zip'] = path
        if cli_event:
            cli_event.set()
        return 1
    app.send_file = fake_send
    await app.send_directory('peer', str(d), None, cleanup=False)
    # inspect zip to ensure link not included
    import zipfile
    with zipfile.ZipFile(called['zip'], 'r') as z:
        names = z.namelist()
    assert 'link.txt' not in names


@pytest.mark.asyncio
async def test_send_directory_rejects_missing_path(tmp_path):
    app = AkitaZmodemMeshCore()
    app.mesh = object()
    cli_event = asyncio.Event()

    tid = await app.send_directory(
        'peer',
        str(tmp_path / "missing"),
        cli_event,
    )

    assert tid is None
    assert cli_event.is_set()


@pytest.mark.asyncio
async def test_send_directory_rejects_empty_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    app = AkitaZmodemMeshCore()
    app.mesh = object()
    cli_event = asyncio.Event()

    tid = await app.send_directory('', str(source), cli_event)

    assert tid is None
    assert cli_event.is_set()


@pytest.mark.asyncio
async def test_receive_file_creates_parent_directory(tmp_path):
    app = AkitaZmodemMeshCore()
    dest = tmp_path / "nested" / "incoming.bin"

    tid = await app.receive_file(str(dest), overwrite=True)

    assert tid is not None
    assert dest.parent.is_dir()


@pytest.mark.asyncio
async def test_handle_zmodem_data_ignores_non_start_payload(tmp_path):
    app = AkitaZmodemMeshCore()
    app.mesh = object()
    dest = tmp_path / "incoming.bin"
    tid = await app.receive_file(str(dest), overwrite=True)

    await app._handle_zmodem_data('peer', b'not-zmodem')

    assert tid in app.transfers
    assert app.transfers[tid]["state"] == "waiting"
    assert not dest.exists()


@pytest.mark.asyncio
async def test_receive_directory_rejects_file_destination(tmp_path):
    app = AkitaZmodemMeshCore()
    dest = tmp_path / "not_a_dir"
    dest.write_text("occupied")
    cli_event = asyncio.Event()

    tid = await app.receive_directory(
        str(dest),
        overwrite=True,
        cli_event=cli_event,
    )

    assert tid is None
    assert cli_event.is_set()


@pytest.mark.asyncio
async def test_receive_directory_uses_internal_temp_even_without_overwrite(
        tmp_path,
        monkeypatch):
    app = AkitaZmodemMeshCore()
    dest = tmp_path / "dir_out"
    captured = {}

    async def fake_receive_file(filepath, overwrite=False, cli_event=None):
        captured['overwrite'] = overwrite
        if cli_event:
            cli_event.set()
        return None

    monkeypatch.setattr(app, "receive_file", fake_receive_file)
    cli_event = asyncio.Event()

    await app.receive_directory(
        str(dest),
        overwrite=False,
        cli_event=cli_event,
    )

    assert captured['overwrite'] is True
    assert cli_event.is_set()
