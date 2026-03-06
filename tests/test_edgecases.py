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


@pytest.mark.asyncio
async def test_handle_zmodem_data_open_failure(tmp_path, monkeypatch):
    # Simulate failing to open file by making path a directory
    app = AkitaZmodemMeshCore()
    app.mesh = object()
    # prepare settings to trigger error in _handle_zmodem_data
    tid = await app.receive_file(str(tmp_path), overwrite=True)
    # craft a fake payload that _handle_zmodem_data will accept
    # monkeypatch zmodem.Receiver to simple object

    class DummyRecv:
        def __init__(self, f): pass
        def receive(self, data): return b''
        def is_finished(self): return False
    monkeypatch.setattr('zmodem.Receiver', DummyRecv)
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
