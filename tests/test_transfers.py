import asyncio
import os
import shutil
import sys
import types

import pytest

# Inject mocks into sys.modules so the main module picks them up when importing
from tests.mocks import MockMesh, MockSender, MockReceiver

TEST_DIR = os.path.join(os.path.dirname(__file__), "tmp_test_dir")


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    # Ensure working test dir
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)

    # Clean any leftover temp zips from previous runs
    for f in os.listdir(os.getcwd()):
        if f.endswith('.zip') and f.startswith('akita_'):
            try:
                os.remove(f)
            except Exception:
                pass

    # Create a small file to send
    with open(os.path.join(TEST_DIR, "file1.txt"), "wb") as f:
        f.write(b"hello world")

    # Monkeypatch meshcore and zmodem modules
    mock_mesh_mod = types.SimpleNamespace(MeshCore=MockMesh, EventType=types.SimpleNamespace(CONTACT_MSG_RECV=1, ERROR=2))
    monkeypatch.setitem(sys.modules, 'meshcore', mock_mesh_mod)

    # zmodem module
    def mock_sender_factory(fobj):
        return MockSender(fobj)

    def mock_receiver_factory(fobj):
        return MockReceiver(fobj)

    mock_zmod = types.SimpleNamespace(Sender=mock_sender_factory, Receiver=mock_receiver_factory)
    monkeypatch.setitem(sys.modules, 'zmodem', mock_zmod)

    yield

    # teardown
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)


@pytest.mark.asyncio
async def test_send_directory_creates_and_cleans_zip(monkeypatch):
    from akita_zmodem_meshcore import AkitaZmodemMeshCore

    app = AkitaZmodemMeshCore()
    # attach mock mesh
    app.mesh = MockMesh()

    cli_event = asyncio.Event()
    await app.send_directory('destnode', TEST_DIR, cli_event)

    # wait for cli_event to be set (send_directory sets it when done)
    await asyncio.wait_for(cli_event.wait(), timeout=5.0)

    # no zip files should remain in cwd
    zips = [f for f in os.listdir(os.getcwd()) if f.endswith('.zip') and 'akita_' in f]
    assert len(zips) == 0


@pytest.mark.asyncio
async def test_send_file_succeeds(monkeypatch, tmp_path):
    from akita_zmodem_meshcore import AkitaZmodemMeshCore

    # create a small file
    fp = tmp_path / "small.bin"
    fp.write_bytes(b"0123456789")

    app = AkitaZmodemMeshCore()
    app.mesh = MockMesh()

    cli_event = asyncio.Event()
    tid = await app.send_file('destnode', str(fp), cli_event)
    assert tid is not None

    # wait for send to complete
    await asyncio.wait_for(cli_event.wait(), timeout=5.0)

    # transfer should be removed from transfers
    assert tid not in app.transfers
