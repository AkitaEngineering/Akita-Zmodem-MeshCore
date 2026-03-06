import asyncio
import os
import shutil
import sys
import types
import json
import struct

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
    mock_mesh_mod = types.SimpleNamespace(
        MeshCore=MockMesh, EventType=types.SimpleNamespace(
            CONTACT_MSG_RECV=1, ERROR=2))
    monkeypatch.setitem(sys.modules, 'meshcore', mock_mesh_mod)

    # zmodem module
    def mock_sender_factory(fobj, *args, **kwargs):
        # ignore chunk_size or any extra arguments; tests only care that it
        # returns an object with the same interface as the real sender
        return MockSender(fobj)

    def mock_receiver_factory(fobj):
        return MockReceiver(fobj)

    mock_zmod = types.SimpleNamespace(
        Sender=mock_sender_factory,
        Receiver=mock_receiver_factory)
    monkeypatch.setitem(sys.modules, 'zmodem', mock_zmod)

    yield

    # teardown
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)


@pytest.mark.asyncio
async def test_send_directory_creates_and_cleans_zip(monkeypatch):
    from akita_zmodem_meshcore import AkitaZmodemMeshCore
    import tempfile

    app = AkitaZmodemMeshCore()
    # attach mock mesh
    app.mesh = MockMesh()

    # record the temporary path used by the method
    tmp_path_holder = {}
    real_ntf = tempfile.NamedTemporaryFile

    def fake_ntf(*args, **kwargs):
        tf = real_ntf(*args, **kwargs)
        tmp_path_holder['name'] = tf.name
        return tf

    monkeypatch.setattr(tempfile, 'NamedTemporaryFile', fake_ntf)

    cli_event = asyncio.Event()
    await app.send_directory('destnode', TEST_DIR, cli_event)

    # wait for cli_event to be set (send_directory sets it when done)
    await asyncio.wait_for(cli_event.wait(), timeout=5.0)

    # ensure the temporary zip (wherever it was created) has been deleted
    zp = tmp_path_holder.get('name')
    assert zp is not None, "temporary zip path should have been recorded"
    assert not os.path.exists(zp), "temporary zip should be removed"


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


@pytest.mark.asyncio
async def test_receive_file_writes_and_cleans(tmp_path):
    from akita_zmodem_meshcore import AkitaZmodemMeshCore, APP_PORT_HEADER_FORMAT

    app = AkitaZmodemMeshCore()
    app.mesh = MockMesh()

    dest = tmp_path / "incoming.bin"
    cli_event = asyncio.Event()
    tid = await app.receive_file(str(dest), overwrite=True, cli_event=cli_event)
    assert tid is not None

    # start the background listener (CLI would have done this automatically)
    asyncio.create_task(app._receive_loop_processor())

    # push a mesh packet with the application port header
    header = struct.pack(APP_PORT_HEADER_FORMAT, app.zmodem_app_port)
    await app._mesh_receive_queue.put({"source": "node123", "data": header + b"MOCKDATA"})

    # the listener should eventually process the packet and complete the
    # transfer, setting the CLI event
    await asyncio.wait_for(cli_event.wait(), timeout=1.0)
    # file should now exist and contain data written by the mock receiver
    assert dest.exists()
    assert dest.stat().st_size > 0
    assert tid not in app.transfers


@pytest.mark.asyncio
async def test_receive_directory_extracts(monkeypatch, tmp_path):
    from akita_zmodem_meshcore import AkitaZmodemMeshCore
    import zipfile

    app = AkitaZmodemMeshCore()
    app.mesh = MockMesh()

    destdir = tmp_path / "dir_out"

    # monkeypatch the receive_file method to simulate a completed transfer. It
    # will write a zip file at the path passed, set the event, and return a
    # dummy tid.
    async def fake_receive_file(self, filepath, overwrite, ev):
        # create a simple zip containing one file
        with zipfile.ZipFile(filepath, "w") as zf:
            zf.writestr("hello.txt", "world")
        ev.set()
        return 1

    monkeypatch.setattr(AkitaZmodemMeshCore, "receive_file", fake_receive_file)

    cli_event = asyncio.Event()
    await app.receive_directory(str(destdir), overwrite=True, cli_event=cli_event)

    # after the method returns the cli_event should already be set
    await asyncio.wait_for(cli_event.wait(), timeout=1.0)

    # the temporary zip must have been removed and directory extracted
    found = list(destdir.rglob("hello.txt"))
    assert found, "extracted file should exist"


def test_configuration_file_override(tmp_path):
    # write a custom config file and ensure the value is picked up when
    # provided to the constructor
    cfg_path = tmp_path / "mycfg.json"
    custom = {"zmodem_app_port": 54321}
    with open(cfg_path, "w") as f:
        json.dump(custom, f)

    from akita_zmodem_meshcore import AkitaZmodemMeshCore
    app = AkitaZmodemMeshCore(config_file=str(cfg_path))
    assert app.zmodem_app_port == 54321


def _simulate_zmodem_exchange(sender, receiver):
    """Helper loop to exchange packets until both sides are finished."""
    # both sides may generate responses that need to be fed back
    while not (sender.is_finished() and receiver.is_finished()):
        pkt = sender.get_next_packet()
        if pkt:
            resp = receiver.receive(pkt)
            if resp:
                sender.receive(resp)
        # break if both idle
        if sender.is_finished() and receiver.is_finished():
            break


def test_zmodem_basic_roundtrip(tmp_path):
    # create source file
    src = tmp_path / "orig.bin"
    data = b"The quick brown fox jumps over the lazy dog" * 100
    src.write_bytes(data)

    # destination file
    dst = tmp_path / "recv.bin"
    # open file objects
    # import the built-in implementation directly (autouse monkeypatch
    # earlier replaces "zmodem" with a mock for the CLI tests)
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        'builtin_zmodem', os.path.join(os.getcwd(), 'zmodem.py'))
    zm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(zm)
    Sender, Receiver = zm.Sender, zm.Receiver
    with open(src, "rb") as sf, open(dst, "wb") as df:
        s = Sender(sf, chunk_size=128)
        r = Receiver(df)
        _simulate_zmodem_exchange(s, r)

    assert dst.read_bytes() == data


def test_zmodem_resume(tmp_path):
    # initial write to destination to simulate partial receive
    dst = tmp_path / "recv2.bin"
    original = b"0123456789" * 1000
    # write first half
    first_half = original[:len(original) // 2]
    with open(dst, "wb") as f:
        f.write(first_half)

    # now run protocol again; sender must resume
    src = tmp_path / "orig2.bin"
    src.write_bytes(original)
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        'builtin_zmodem', os.path.join(os.getcwd(), 'zmodem.py'))
    zm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(zm)
    Sender, Receiver = zm.Sender, zm.Receiver
    with open(src, "rb") as sf, open(dst, "ab") as df:
        # create receiver pointing at the file we already partially filled
        r = Receiver(df)
        s = Sender(sf, chunk_size=256)
        _simulate_zmodem_exchange(s, r)

    assert dst.read_bytes() == original


@pytest.mark.asyncio
async def test_end_to_end_app_transfer(tmp_path):
    # build two apps with a simple in-memory mesh linking them
    from akita_zmodem_meshcore import EventType
    import types

    class PairMesh:
        def __init__(self):
            self.commands = self
            self.subscriptions = {}
            self.other = None

        async def send_msg(self, destination, payload):
            # debug
            print(f"[PairMesh] sending {len(payload)} bytes to {destination}")
            if self.other:
                ev = types.SimpleNamespace(
                    payload={
                        "decoded": {
                            "payload": payload},
                        "from": "peer"})
                for cb in self.other.subscriptions.get(
                        EventType.CONTACT_MSG_RECV, []):
                    await cb(ev)

        def subscribe(self, event, callback):
            self.subscriptions.setdefault(event, []).append(callback)

        async def close(self):
            return True

    # make sure the application imports the real zmodem implementation
    import importlib
    import sys
    import os
    if 'zmodem' in sys.modules:
        del sys.modules['zmodem']
    spec = importlib.util.spec_from_file_location(
        'zmodem', os.path.join(os.getcwd(), 'zmodem.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules['zmodem'] = module
    # force reload of application module so it picks up the real zmodem
    import akita_zmodem_meshcore
    importlib.reload(akita_zmodem_meshcore)

    # instantiate apps and connect meshes
    app1 = akita_zmodem_meshcore.AkitaZmodemMeshCore()
    app2 = akita_zmodem_meshcore.AkitaZmodemMeshCore()
    print("app1 mesh_packet_chunk_size", app1.mesh_packet_chunk_size)
    print("app2 mesh_packet_chunk_size", app2.mesh_packet_chunk_size)
    m1 = PairMesh()
    m2 = PairMesh()
    m1.other = m2
    m2.other = m1
    app1.mesh = m1
    app2.mesh = m2
    # manually register callbacks normally done in _connect_mesh
    m1.subscribe(EventType.CONTACT_MSG_RECV, app1._on_mesh_message)
    m1.subscribe(EventType.ERROR, app1._on_mesh_error)
    m2.subscribe(EventType.CONTACT_MSG_RECV, app2._on_mesh_message)
    m2.subscribe(EventType.ERROR, app2._on_mesh_error)

    # prepare files
    filedata = b"hello world" * 100
    src = tmp_path / "file.txt"
    src.write_bytes(filedata)
    dest = tmp_path / "received.txt"

    # start background loops on both
    import logging
    logging.getLogger().setLevel(logging.DEBUG)
    asyncio.create_task(app1._receive_loop_processor())
    asyncio.create_task(app1._timeout_check())
    asyncio.create_task(app2._receive_loop_processor())
    asyncio.create_task(app2._timeout_check())

    cli_event2 = asyncio.Event()
    await app2.receive_file(str(dest), overwrite=True, cli_event=cli_event2)

    cli_event1 = asyncio.Event()
    await app1.send_file('peer', str(src), cli_event1)

    await asyncio.wait_for(cli_event1.wait(), timeout=5.0)
    await asyncio.wait_for(cli_event2.wait(), timeout=5.0)

    assert dest.read_bytes() == filedata
