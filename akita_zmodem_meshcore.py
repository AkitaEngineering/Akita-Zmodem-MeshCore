#!/usr/bin/env python3
import tempfile
import atexit
import zmodem
import asyncio
import logging
import os
import zipfile
import argparse
import json
import time
import struct
import signal
import sys
import hashlib

# -----------------------------------------------------------------------------
# Dependency Validation
# -----------------------------------------------------------------------------


def check_dependency(module_name, pip_name):
    try:
        __import__(module_name)
    except ImportError:
        print(f"CRITICAL ERROR: '{module_name}' library not found.")
        print(f"Please install it: pip install {pip_name}")
        sys.exit(1)


# Attempt to import MeshCore; allow module import even if the dependency
# is not installed so tests and documentation can be generated.  If missing
# the concrete `_connect_mesh` call will fail at runtime with a clear error.
try:
    from meshcore import MeshCore, EventType
except Exception:
    MeshCore = None

    class EventType:
        CONTACT_MSG_RECV = "contact_msg_recv"
        ERROR = "error"


# global list for any temp zips created by any instance; cleaned at exit
_temp_zip_files = []


def _cleanup_temp_zips():
    for fp in list(_temp_zip_files):
        try:
            if os.path.exists(fp):
                os.remove(fp)
        except Exception:
            pass


atexit.register(_cleanup_temp_zips)


class UnsafeZipError(Exception):
    """Raised when a zip archive contains unsafe member paths (ZipSlip)."""


# Optional: TQDM for progress bars
try:
    from tqdm.asyncio import tqdm
    TQDM_AVAILABLE = True
except Exception:
    TQDM_AVAILABLE = False
    try:
        print(
            "Suggestion: Install 'tqdm' for progress bars (pip install tqdm)",
            file=sys.stderr)
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------
CONFIG_FILE = "akita_zmodem_meshcore_config.json"
DEFAULT_CONFIG = {
    "zmodem_app_port": 2001,
    # "chunk_size" is included for completeness but is unused by the
    # current implementation; some third‑party zmodem wrappers expose a
    # chunk size parameter so we keep the value here for compatibility.
    "chunk_size": 256,             # Internal Zmodem buffer size (unused)
    # Max payload per mesh packet (LoRa MTU safe)
    "mesh_packet_chunk_size": 200,
    "timeout": 120,                # Extended timeout for slow links
    "mesh_connection_type": "serial",
    "mesh_serial_port": "/dev/ttyUSB0",
    "mesh_serial_baud": 115200,
    "mesh_tcp_host": "127.0.0.1",
    "mesh_tcp_port": 4403,
    "tx_delay_ms": 150             # Throttle to prevent radio buffer saturation
}

APP_PORT_HEADER_FORMAT = "!H"
APP_PORT_HEADER_SIZE = struct.calcsize(APP_PORT_HEADER_FORMAT)

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def load_config(config_file: str = None):
    """Return configuration dictionary from the given JSON file.

    If the file does not exist or is not valid JSON we overwrite it with the
    default settings.  The caller may supply an alternate path (for example
    when the CLI ``--config`` argument is used).
    """
    if config_file is None:
        config_file = CONFIG_FILE

    try:
        with open(config_file, "r") as f:
            loaded = json.load(f)
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(loaded)
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        try:
            with open(config_file, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
        except Exception:
            # best effort; if we can’t write the file just continue with
            # defaults – the user can fix permissions manually.
            pass
        return DEFAULT_CONFIG


# The constants below are populated lazily from the first instance so that
# CLI overrides (and alternate config file paths) work correctly.  They remain
# here primarily for backward compatibility with external code or tests that
# import them directly.
config_data = None
ZMODEM_APP_PORT = None
MESH_PACKET_CHUNK_SIZE = None
TIMEOUT = None
TX_DELAY_S = None


def calculate_md5(filepath):
    """Calculates MD5 checksum of a file for integrity verification.

    This is a blocking operation and should generally be executed in a
    background thread (e.g. via ``asyncio.to_thread``) when called from an
    async context.
    """
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def _safe_extract_zip(zip_path, extract_to):
    """Safely extract a zip file into `extract_to` preventing ZipSlip.

    Streams entry names to avoid loading an entire name list into memory.
    Raises an Exception if any member would extract outside `extract_to`.
    """
    import zipfile
    import os

    with zipfile.ZipFile(zip_path, 'r') as z:
        for info in z.infolist():
            member = info.filename
            normalized = os.path.normpath(member)
            if normalized.startswith('..') or os.path.isabs(normalized):
                raise UnsafeZipError(f"Unsafe path in zip archive: {member}")
            dest_path = os.path.join(extract_to, normalized)
            abs_dest = os.path.abspath(dest_path)
            abs_base = os.path.abspath(extract_to)
            if not (
                abs_dest == abs_base or abs_dest.startswith(
                    abs_base + os.sep)):
                raise UnsafeZipError(
                    f"Zip would extract outside target: {member}")
            # Ensure directory exists
            parent = os.path.dirname(abs_dest)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            # If this is a directory entry, skip file write
            if member.endswith('/') or info.is_dir():
                continue
            # Stream extract member content to avoid large memory use
            with z.open(info, 'r') as src, open(abs_dest, 'wb') as dst:
                for chunk in iter(lambda: src.read(8192), b''):
                    dst.write(chunk)

# -----------------------------------------------------------------------------
# Core Class
# -----------------------------------------------------------------------------


class AkitaZmodemMeshCore:
    def __init__(self, cli_config_overrides=None, config_file: str = None):
        self.mesh = None
        self.transfers = {}
        self.transfer_id_counter = 0
        self.running = True
        self._mesh_receive_queue = asyncio.Queue()
        self._temp_zips = []  # track temp archives for cleanup
        # register instance temp files in global list for atexit cleanup

        def _register(fp):
            _temp_zip_files.append(fp)
        self._register_temp = _register

        base = load_config(config_file)
        self.app_config = base.copy()
        if cli_config_overrides:
            self.app_config.update(cli_config_overrides)
        self._validate_config()

        # compute frequently-used values once per instance
        self.zmodem_app_port = self.app_config.get(
            "zmodem_app_port", DEFAULT_CONFIG["zmodem_app_port"])
        self.mesh_packet_chunk_size = self.app_config.get(
            "mesh_packet_chunk_size", DEFAULT_CONFIG["mesh_packet_chunk_size"])
        self.timeout = self.app_config.get(
            "timeout", DEFAULT_CONFIG["timeout"])
        self.tx_delay_s = self.app_config.get(
            "tx_delay_ms", DEFAULT_CONFIG["tx_delay_ms"]) / 1000.0

        # update module globals so tests relying on them remain valid
        global config_data, ZMODEM_APP_PORT, MESH_PACKET_CHUNK_SIZE, TIMEOUT, TX_DELAY_S
        config_data = base
        ZMODEM_APP_PORT = self.zmodem_app_port
        MESH_PACKET_CHUNK_SIZE = self.mesh_packet_chunk_size
        TIMEOUT = self.timeout
        TX_DELAY_S = self.tx_delay_s

    def generate_transfer_id(self):
        self.transfer_id_counter += 1
        return self.transfer_id_counter

    def _validate_config(self):
        # ensure numeric configuration values are of the expected type
        fields = [
            ("zmodem_app_port", int),
            ("mesh_packet_chunk_size", int),
            ("timeout", (int, float)),
            ("tx_delay_ms", (int, float)),
        ]
        for key, typ in fields:
            val = self.app_config.get(key)
            if val is not None and not isinstance(val, typ):
                raise ValueError(
                    f"Configuration key '{key}' must be {typ}, got {type(val)}")
            if key in (
                "mesh_packet_chunk_size",
                    "timeout") and val is not None:
                if val <= 0:
                    raise ValueError(f"{key} must be positive")

    async def _connect_mesh(self):
        conn_type = self.app_config.get("mesh_connection_type", "serial")
        # Fail fast with a clear exception when the meshcore Python client
        # is not available. The module import is optional for documentation
        # and testing, but actual operation requires the client.
        if MeshCore is None:
            raise RuntimeError(
                "MeshCore Python client is not installed; install via 'pip install meshcore' "
                "or provide a compatible library.")
        try:
            if conn_type == "serial":
                port = self.app_config.get("mesh_serial_port")
                baud = self.app_config.get("mesh_serial_baud")
                logging.info(f"Connecting Serial: {port} @ {baud}")
                self.mesh = await MeshCore.create_serial(device=port, baud=baud)
            elif conn_type == "tcp":
                host = self.app_config.get("mesh_tcp_host")
                port = self.app_config.get("mesh_tcp_port")
                logging.info(f"Connecting TCP: {host}:{port}")
                self.mesh = await MeshCore.create_tcp(host=host, port=port)

            if self.mesh:
                logging.info("Connected to MeshCore Network.")
                self.mesh.subscribe(
                    EventType.CONTACT_MSG_RECV,
                    self._on_mesh_message)
                self.mesh.subscribe(EventType.ERROR, self._on_mesh_error)
                return True
        except Exception as e:
            logging.error(f"Connection Failed: {e}")
        return False

    async def _on_mesh_message(self, event):
        try:
            payload = event.payload
            # Extract Source ID (compatible with multiple lib versions)
            src = str(payload.get('from_num', payload.get('from', 'unknown')))

            # Extract Data (decoded payload or raw text fallback)
            data = payload.get('decoded', {}).get('payload')
            if not data:
                txt = payload.get('text')
                if isinstance(txt, str):
                    data = txt.encode('utf-8', 'ignore')
                elif isinstance(txt, bytes):
                    data = txt

            if data and isinstance(data, bytes):
                await self._mesh_receive_queue.put({"source": src, "data": data})
        except Exception as e:
            logging.error(f"Msg Parse Error: {e}")

    async def _on_mesh_error(self, event):
        logging.warning(
            f"Mesh Error: {
                event.payload if hasattr(
                    event,
                    'payload') else event}")

    # -------------------------------------------------------------------------
    # Send Logic
    # -------------------------------------------------------------------------
    async def send_file(self, dest_node, filepath, cli_event=None):
        if not self.mesh:
            if cli_event:
                cli_event.set()
            return None
        if os.path.isdir(filepath):
            logging.error(f"Path '{filepath}' is a directory, not a file")
            if cli_event:
                cli_event.set()
            return None
        if not isinstance(dest_node, str) or not dest_node:
            logging.error(f"Invalid destination node: {dest_node}")
            if cli_event:
                cli_event.set()
            return None

        if not os.path.exists(filepath):
            logging.error(f"File not found: {filepath}")
            if cli_event:
                cli_event.set()
            return None

        tid = self.generate_transfer_id()
        fsize = os.path.getsize(filepath)
        # calculate checksum in a thread to avoid blocking the event loop
        checksum = await asyncio.to_thread(calculate_md5, filepath)

        logging.info(
            f"[Tx-{tid}] File: {
                os.path.basename(filepath)} | Size: {
                fsize:,} bytes | MD5: {checksum}")

        try:
            # Open file in thread to avoid blocking loop
            sync_f = await asyncio.to_thread(open, filepath, "rb")
            # let the sender know the configured chunk size (legacy key
            # "chunk_size", kept for compatibility)
            sz = self.app_config.get(
                "chunk_size", DEFAULT_CONFIG["chunk_size"])
            sender = await asyncio.to_thread(zmodem.Sender, sync_f, sz)
        except Exception as e:
            logging.error(f"Zmodem Init Error: {e}")
            if 'sync_f' in locals() and sync_f:
                sync_f.close()
            if cli_event:
                cli_event.set()
            return None

        self.transfers[tid] = {
            "state": "sending", "sender": sender, "sync_f": sync_f,
            "file": filepath, "dest": dest_node, "start": time.time(),
            "last_act": time.time(), "bytes": 0, "total": fsize,
            "cli_event": cli_event
        }

        asyncio.create_task(self._send_loop(tid))
        return tid

    async def _send_loop(self, tid):
        t = self.transfers[tid]
        sender = t["sender"]
        dest = t["dest"]

        # Progress Bar
        pbar = None
        if TQDM_AVAILABLE:
            pbar = tqdm(
                total=t["total"],
                desc=f"Tx-{tid}",
                unit="B",
                unit_scale=True,
                leave=True)

        try:
            while self.running:
                if await asyncio.to_thread(sender.is_finished):
                    logging.info(f"[Tx-{tid}] Transfer Complete.")
                    break

                # Get packet from Zmodem
                packet = await asyncio.to_thread(sender.get_next_packet)

                if packet:
                    logging.debug(
                        f"[Tx-{tid}] next packet size {len(packet)} state={sender.state}")
                    # Construct ZMODEM packet once (packet includes framing)
                    header = struct.pack(
                        APP_PORT_HEADER_FORMAT, self.zmodem_app_port)
                    remaining = packet

                    # Chunking: ensure each chunk begins with header so receiver can
                    # unconditionally strip it.  We account for header size when
                    # slicing the payload portion so chunks stay within the
                    # configured mesh_packet_chunk_size.
                    max_payload = self.mesh_packet_chunk_size - len(header)
                    while remaining:
                        piece = remaining[:max_payload]
                        remaining = remaining[len(piece):]
                        chunk = header + piece
                        try:
                            logging.debug(
                                f"[Tx-{tid}] sending chunk {len(chunk)}")
                            await self.mesh.commands.send_msg(destination=dest, payload=chunk)
                            t["last_act"] = time.time()
                            # Throttle
                            await asyncio.sleep(self.tx_delay_s)
                        except Exception as e:
                            logging.warning(f"[Tx-{tid}] Send Fail: {e}")
                            await asyncio.sleep(1.0)  # Backoff

                    if pbar:
                        pbar.update(len(packet))
                else:
                    await asyncio.sleep(0.1)

        except Exception as e:
            logging.error(f"[Tx-{tid}] Error: {e}")
        finally:
            if pbar:
                pbar.close()
            self.cancel_transfer(tid)

    # -------------------------------------------------------------------------
    # Receive Logic
    # -------------------------------------------------------------------------
    async def receive_file(self, filepath, overwrite=False, cli_event=None):
        if os.path.exists(filepath) and not overwrite:
            logging.error(f"File exists: {filepath} (Use --overwrite)")
            if cli_event:
                cli_event.set()
            return None

        tid = self.generate_transfer_id()
        self.transfers[tid] = {
            "state": "waiting", "receiver": None, "sync_f": None,
            "file": filepath, "dest": None, "start": time.time(),
            "last_act": time.time(), "bytes": 0,
            "cli_event": cli_event
        }
        logging.info(f"[Rx-{tid}] Listening... Destination: {filepath}")
        return tid

    async def _receive_loop_processor(self):
        logging.info("Daemon: Packet Listener Active")
        while self.running:
            try:
                item = await asyncio.wait_for(self._mesh_receive_queue.get(), timeout=1.0)
                src = item["source"]
                data = item["data"]

                if len(data) <= APP_PORT_HEADER_SIZE:
                    continue

                port = struct.unpack(APP_PORT_HEADER_FORMAT,
                                     data[:APP_PORT_HEADER_SIZE])[0]
                if port == self.zmodem_app_port:
                    # strip header and deliver to protocol handler
                    await self._handle_zmodem_data(src, data[APP_PORT_HEADER_SIZE:])

            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logging.error(f"Listener Error: {e}")

    async def _handle_zmodem_data(self, src, data):
        active_tid = None
        # scan transfers and, if this is a new incoming stream, initialize it
        for tid, t in list(self.transfers.items()):
            if t["state"] == "waiting":
                # Do not pre-open the destination file (which could truncate
                # it). Instead create a Receiver that manages its own file
                # handle and resume detection based on the filepath.
                t["dest"] = src
                t["state"] = "receiving"
                t["sync_f"] = None
                try:
                    t["receiver"] = await asyncio.to_thread(zmodem.Receiver, t["file"])
                except Exception as e:
                    logging.error(
                        f"[Rx-{tid}] Cannot init receiver for '{t['file']}': {e}")
                    self.cancel_transfer(tid)
                    continue
                logging.info(f"[Rx-{tid}] Incoming stream from {src} accepted")
                active_tid = tid
                break
            elif t["state"] == "receiving" and t.get("dest") == src:
                active_tid = tid
                break
            elif t["state"] == "sending" and t.get("dest") == src:
                active_tid = tid
                break
        if not active_tid:
            return
        t = self.transfers[active_tid]
        t["last_act"] = time.time()

        if t["state"] == "receiving":
            receiver = t["receiver"]
            try:
                logging.debug(
                    f"[Rx-{active_tid}] delivering {len(data)} bytes to receiver")
                resp = await asyncio.to_thread(receiver.receive, data)
                t["bytes"] += len(data)
                logging.debug(
                    f"[Rx-{active_tid}] receiver state={
                        receiver.state} resp_len={
                        len(resp) if resp else 0}")

                if resp:
                    resp_payload = struct.pack(
                        APP_PORT_HEADER_FORMAT, self.zmodem_app_port) + resp
                    logging.debug(
                        f"[Rx-{active_tid}] sending {len(resp)} bytes back")
                    await self.mesh.commands.send_msg(destination=src, payload=resp_payload)

                if await asyncio.to_thread(receiver.is_finished):
                    logging.info(f"[Rx-{active_tid}] Transfer Complete.")
                    checksum = await asyncio.to_thread(calculate_md5, t["file"])
                    logging.info(
                        f"[Rx-{active_tid}] File Saved. MD5: {checksum}")
                    self.cancel_transfer(active_tid)
            except Exception as e:
                logging.error(f"[Rx-{active_tid}] Zmodem Protocol Error: {e}")
                self.cancel_transfer(active_tid)

        elif t["state"] == "sending" and t.get("sender"):
            try:
                logging.debug(
                    f"[Tx-{active_tid}] delivering {len(data)} bytes to sender")
                resp = await asyncio.to_thread(t["sender"].receive, data)
                logging.debug(
                    f"[Tx-{active_tid}] sender returned {len(resp) if resp else 0} bytes")
                if resp:
                    resp_payload = struct.pack(
                        APP_PORT_HEADER_FORMAT, self.zmodem_app_port) + resp
                    await self.mesh.commands.send_msg(destination=src, payload=resp_payload)
            except Exception as e:
                logging.error(f"[Tx-{active_tid}] Protocol error: {e}")
    # -------------------------------------------------------------------------
    # Directory Handling & Management
    # -------------------------------------------------------------------------

    async def send_directory(self, dest, path, cli_event, cleanup=True):
        # create temporary zip file in system temp directory to avoid cluttering
        # the working directory; the file is deleted when the transfer finishes
        # (or on error).
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            zip_name = tf.name
        logging.info(f"Compressing directory '{path}' into {zip_name}...")

        def _zip():
            with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(path):
                    for file in files:
                        p = os.path.join(root, file)
                        # skip symlinks to avoid unintentionally archiving
                        # system paths
                        if os.path.islink(p):
                            logging.debug(
                                f"Skipping symlink {p} in directory transfer")
                            continue
                        z.write(p, os.path.relpath(p, path))

        try:
            await asyncio.to_thread(_zip)
        except Exception as e:
            logging.error(f"Error compressing directory '{path}': {e}")
            # cleanup temp file if it exists
            try:
                if os.path.exists(zip_name):
                    os.remove(zip_name)
            except Exception:
                pass
            if cli_event:
                cli_event.set()
            return None

        f_event = asyncio.Event()
        tid = await self.send_file(dest, zip_name, f_event)

        try:
            if tid:
                await f_event.wait()
        finally:
            # Ensure the temporary zip is removed where possible
            if cleanup:
                try:
                    if os.path.exists(zip_name):
                        os.remove(zip_name)
                except Exception as e:
                    logging.warning(
                        f"Failed to remove temp zip '{zip_name}': {e}")
            if cli_event:
                try:
                    cli_event.set()
                except Exception:
                    pass

    async def receive_directory(
            self,
            path,
            overwrite,
            cli_event,
            cleanup=True):
        if not os.path.exists(path):
            os.makedirs(path)
        # create a secure temporary file for the incoming zip to avoid
        # predictable filenames and TOCTOU issues
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            zip_name = tf.name

        f_event = asyncio.Event()
        tid = await self.receive_file(zip_name, overwrite, f_event)
        if tid:
            self._temp_zips.append(zip_name)
            self._register_temp(zip_name)
            await f_event.wait()
            if os.path.exists(zip_name):
                logging.info(f"Extracting to '{path}'...")
                try:
                    await asyncio.to_thread(_safe_extract_zip, zip_name, path)
                except UnsafeZipError as e:
                    logging.error(f"Received zip rejected as unsafe: {e}")
                except Exception as e:
                    logging.error(
                        f"Failed to extract received zip '{zip_name}': {e}")
                finally:
                    if cleanup:
                        try:
                            os.remove(zip_name)
                        except BaseException:
                            pass
        if cli_event:
            cli_event.set()

    def cancel_transfer(self, tid):
        if tid in self.transfers:
            t = self.transfers.pop(tid)
            # Close any file handles in a thread to avoid blocking the loop
            if t.get("sync_f"):
                try:
                    f = t["sync_f"]
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop and loop.is_running():
                        # schedule a threaded close
                        asyncio.create_task(asyncio.to_thread(f.close))
                    else:
                        try:
                            f.close()
                        except Exception:
                            pass
                except Exception:
                    pass
            # If receiver exists and holds an open file, close that safely
            if t.get("receiver"):
                try:
                    rcv = t["receiver"]
                    if hasattr(rcv, 'fobj') and rcv.fobj:
                        try:
                            loop = None
                            try:
                                loop = asyncio.get_running_loop()
                            except RuntimeError:
                                loop = None
                            if loop and loop.is_running():
                                asyncio.create_task(
                                    asyncio.to_thread(rcv.fobj.close))
                            else:
                                try:
                                    rcv.fobj.close()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass
            if t.get("cli_event"):
                t["cli_event"].set()
            return True
        return False

    async def _timeout_check(self):
        while self.running:
            now = time.time()
            for tid in list(self.transfers.keys()):
                if now - self.transfers[tid]["last_act"] > self.timeout:
                    logging.warning(f"[Tx/Rx-{tid}] Timeout. Cancelling.")
                    self.cancel_transfer(tid)
            await asyncio.sleep(5)

    async def stop(self):
        self.running = False
        for tid in list(self.transfers.keys()):
            self.cancel_transfer(tid)
        if self.mesh:
            try:
                await self.mesh.close()
            except BaseException:
                pass

# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(description="Akita-Zmodem-MeshCore")
    parser.add_argument(
        "--config",
        default=CONFIG_FILE,
        help="Path to configuration JSON file (will be created if missing)")
    parser.add_argument("--mesh-type", choices=["serial", "tcp"],
                        help="Override the connection type from config")
    parser.add_argument(
        "--serial-port",
        help="Serial device path (for serial)")
    parser.add_argument("--serial-baud", type=int, help="Serial baud rate")
    parser.add_argument("--tcp-host", dest="tcp_host",
                        help="TCP host for meshcore connection (tcp)")
    parser.add_argument("--tcp-port", dest="tcp_port", type=int,
                        help="TCP port for meshcore connection (tcp)")

    sub = parser.add_subparsers(dest="command")

    p_send = sub.add_parser("send")
    p_send.add_argument("dest", help="Dest Node ID")
    p_send.add_argument("path", help="File/Dir path")

    p_recv = sub.add_parser("receive")
    p_recv.add_argument("path", help="Save path (directory or filename)")
    p_recv.add_argument("--overwrite", action="store_true")
    p_recv.add_argument("--directory", action="store_true",
                        help="Force treat the destination as a directory")

    sub.add_parser("status").add_argument("id", type=int)
    sub.add_parser("cancel").add_argument("id", type=int)

    args = parser.parse_args()

    overrides = {}
    if args.mesh_type:
        overrides["mesh_connection_type"] = args.mesh_type
    if args.serial_port:
        overrides["mesh_serial_port"] = args.serial_port
    if args.serial_baud is not None:
        overrides["mesh_serial_baud"] = args.serial_baud
    if args.tcp_host:
        overrides["mesh_tcp_host"] = args.tcp_host
    if args.tcp_port is not None:
        overrides["mesh_tcp_port"] = args.tcp_port

    app = AkitaZmodemMeshCore(overrides, config_file=args.config)

    # Clean Exit
    def sig_handler():
        logging.info("Interrupted. Stopping...")
        asyncio.create_task(app.stop())
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, sig_handler)
    except BaseException:
        pass

    try:
        if not await app._connect_mesh():
            return
    except Exception as e:
        logging.error(f"Failed to connect to mesh: {e}")
        return

    # start background processors in all modes; send-only operations will
    # simply sit idle, but receive commands and the daemon depend on them.
    asyncio.create_task(app._receive_loop_processor())
    asyncio.create_task(app._timeout_check())

    cli_event = asyncio.Event()

    try:
        if args.command == "send":
            if os.path.isdir(args.path):
                await app.send_directory(args.dest, args.path, cli_event)
            else:
                await app.send_file(args.dest, args.path, cli_event)
            await cli_event.wait()

        elif args.command == "receive":
            is_dir = getattr(
                args,
                "directory",
                False) or os.path.isdir(
                args.path)
            if is_dir:
                await app.receive_directory(args.path, args.overwrite, cli_event)
            else:
                await app.receive_file(args.path, args.overwrite, cli_event)
            await cli_event.wait()

        elif args.command == "status":
            print(json.dumps(app.transfers, default=str, indent=2))

        elif args.command == "cancel":
            app.cancel_transfer(args.id)

        else:
            # Daemon -- the work loops are already running above.  simply sleep
            logging.info(f"Daemon Listening. ID: {app.mesh} (Ctrl+C to stop)")
            while app.running:
                await asyncio.sleep(1)

    except asyncio.CancelledError:
        pass
    finally:
        await app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
