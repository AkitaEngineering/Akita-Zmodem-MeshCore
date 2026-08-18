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

from mesh_transport import (
    MESHCORE_MAX_BINARY_CHUNK,
    encode_mesh_message,
    is_command_error,
    parse_inbound_event,
    peer_matches,
    resolve_destination,
    sanitize_filename,
    sender_is_allowed,
)

__version__ = "1.0.0"

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
MAX_ZMODEM_CHUNK_SIZE = 4096
APP_PORT_HEADER_FORMAT = "!H"
APP_PORT_HEADER_SIZE = struct.calcsize(APP_PORT_HEADER_FORMAT)
MIN_MESH_PACKET_CHUNK_SIZE = APP_PORT_HEADER_SIZE + 16
DEFAULT_INCOMING_DIR = "incoming"
DEFAULT_MAX_FILE_SIZE_BYTES = 1048576
DEFAULT_CONFIG = {
    "zmodem_app_port": 2001,
    # Internal protocol data block size. Mesh packets are still fragmented
    # further according to "mesh_packet_chunk_size".
    "chunk_size": 256,
    # Binary size per mesh text message, including the 2-byte app-port
    # header, before AZM1/base64 encoding. Must fit a MeshCore TXT_MSG.
    "mesh_packet_chunk_size": MESHCORE_MAX_BINARY_CHUNK,
    "timeout": 120,                # Extended timeout for slow links
    "retransmit_timeout_s": 8,
    "mesh_connection_type": "serial",
    "mesh_serial_port": "/dev/ttyUSB0",
    "mesh_serial_baud": 115200,
    "mesh_tcp_host": "127.0.0.1",
    "mesh_tcp_port": 4403,
    "tx_delay_ms": 150,            # Throttle to prevent radio saturation
    "min_tx_delay_ms": 50,
    "allow_unsafe_tx_delay": False,
    "max_consecutive_send_failures": 5,
    "max_inbound_queue": 128,
    "max_file_size_bytes": DEFAULT_MAX_FILE_SIZE_BYTES,
    "auto_receive": True,
    "incoming_dir": DEFAULT_INCOMING_DIR,
    "allowed_senders": [],
    "control_host": "127.0.0.1",
    "control_port": 8765
}

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

    A missing file is created with defaults. Invalid JSON is left untouched
    so a typo cannot wipe a working configuration.
    """
    if config_file is None:
        config_file = CONFIG_FILE

    try:
        with open(config_file, "r") as f:
            loaded = json.load(f)
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(loaded)
            return cfg
    except FileNotFoundError:
        try:
            with open(config_file, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
        except OSError:
            pass
        return DEFAULT_CONFIG.copy()
    except json.JSONDecodeError as e:
        logging.error(
            "Invalid JSON in %s: %s; using defaults (file not overwritten)",
            config_file,
            e)
        return DEFAULT_CONFIG.copy()


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


def _safe_extract_zip(
        zip_path, extract_to, overwrite=True, max_total_bytes=None):
    """Safely extract a zip file into `extract_to` preventing ZipSlip.

    Streams entry names to avoid loading an entire name list into memory.
    Raises an Exception if any member would extract outside `extract_to`, or
    if `overwrite` is false and an archive member would replace a file.
    """
    import zipfile
    import os

    with zipfile.ZipFile(zip_path, 'r') as z:
        members = []
        advertised = 0
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
            if not overwrite and not (member.endswith('/') or info.is_dir()):
                if os.path.exists(abs_dest):
                    raise FileExistsError(
                        f"Zip member would overwrite existing file: {member}")
            advertised += max(info.file_size, 0)
            if max_total_bytes and advertised > max_total_bytes:
                raise UnsafeZipError(
                    "Zip uncompressed size exceeds max_file_size_bytes")
            members.append((info, member, abs_dest))

        written = 0
        for info, member, abs_dest in members:
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
                    written += len(chunk)
                    if max_total_bytes and written > max_total_bytes:
                        raise UnsafeZipError(
                            "Zip uncompressed size exceeds "
                            "max_file_size_bytes")
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
        self._mesh_receive_queue = None
        self._temp_zips = []  # track temp archives for cleanup
        self._control_server = None
        # register instance temp files in global list for atexit cleanup

        def _register(fp):
            _temp_zip_files.append(fp)
        self._register_temp = _register

        base = load_config(config_file)
        self.app_config = base.copy()
        if cli_config_overrides:
            self.app_config.update(cli_config_overrides)
        self._validate_config()
        self._mesh_receive_queue = asyncio.Queue(
            maxsize=self.app_config.get(
                "max_inbound_queue",
                DEFAULT_CONFIG["max_inbound_queue"]))

        # compute frequently-used values once per instance
        self.zmodem_app_port = self.app_config.get(
            "zmodem_app_port", DEFAULT_CONFIG["zmodem_app_port"])
        self.mesh_packet_chunk_size = self.app_config.get(
            "mesh_packet_chunk_size", DEFAULT_CONFIG["mesh_packet_chunk_size"])
        self.timeout = self.app_config.get(
            "timeout", DEFAULT_CONFIG["timeout"])
        self.tx_delay_s = self.app_config.get(
            "tx_delay_ms", DEFAULT_CONFIG["tx_delay_ms"]) / 1000.0
        self.max_consecutive_send_failures = self.app_config.get(
            "max_consecutive_send_failures",
            DEFAULT_CONFIG["max_consecutive_send_failures"])
        self.max_file_size_bytes = self.app_config.get(
            "max_file_size_bytes", DEFAULT_CONFIG["max_file_size_bytes"])
        self.retransmit_timeout_s = self.app_config.get(
            "retransmit_timeout_s", DEFAULT_CONFIG["retransmit_timeout_s"])
        self.auto_receive = self.app_config.get(
            "auto_receive", DEFAULT_CONFIG["auto_receive"])
        self.incoming_dir = self.app_config.get(
            "incoming_dir", DEFAULT_CONFIG["incoming_dir"])
        self.allowed_senders = list(
            self.app_config.get(
                "allowed_senders", DEFAULT_CONFIG["allowed_senders"]) or [])

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

    def _require_type(self, key, typ):
        val = self.app_config.get(key)
        if val is not None and not isinstance(val, typ):
            raise ValueError(
                f"Configuration key '{key}' must be {typ}, got {type(val)}")
        return val

    def _validate_config(self):
        positives = (
            "chunk_size",
            "mesh_packet_chunk_size",
            "timeout",
            "retransmit_timeout_s",
            "max_consecutive_send_failures",
            "max_inbound_queue",
        )
        non_negatives = (
            "tx_delay_ms",
            "min_tx_delay_ms",
            "max_file_size_bytes",
        )
        fields = [
            ("zmodem_app_port", int),
            ("chunk_size", int),
            ("mesh_packet_chunk_size", int),
            ("timeout", (int, float)),
            ("retransmit_timeout_s", (int, float)),
            ("mesh_serial_baud", int),
            ("mesh_tcp_port", int),
            ("tx_delay_ms", (int, float)),
            ("min_tx_delay_ms", (int, float)),
            ("max_consecutive_send_failures", int),
            ("max_inbound_queue", int),
            ("max_file_size_bytes", int),
            ("control_port", int),
        ]
        for key, typ in fields:
            val = self._require_type(key, typ)
            if val is None:
                continue
            if key in positives and val <= 0:
                raise ValueError(f"{key} must be positive")
            if key in non_negatives and val < 0:
                raise ValueError(f"{key} must be zero or positive")

        app_port = self.app_config.get("zmodem_app_port")
        if app_port is not None and not 0 <= app_port <= 65535:
            raise ValueError("zmodem_app_port must be between 0 and 65535")

        tcp_port = self.app_config.get("mesh_tcp_port")
        if tcp_port is not None and not 1 <= tcp_port <= 65535:
            raise ValueError("mesh_tcp_port must be between 1 and 65535")

        control_port = self.app_config.get("control_port")
        if control_port is not None and not 0 <= control_port <= 65535:
            raise ValueError("control_port must be between 0 and 65535")

        if not isinstance(self.app_config.get("control_host", ""), str):
            raise ValueError("control_host must be a string")
        if not isinstance(
                self.app_config.get("incoming_dir", DEFAULT_INCOMING_DIR),
                str):
            raise ValueError("incoming_dir must be a string")

        conn_type = self.app_config.get("mesh_connection_type")
        if conn_type not in ("serial", "tcp"):
            raise ValueError("mesh_connection_type must be 'serial' or 'tcp'")

        if self.app_config.get("mesh_serial_baud", 1) <= 0:
            raise ValueError("mesh_serial_baud must be positive")

        protocol_chunk_size = self.app_config.get("chunk_size")
        if protocol_chunk_size is not None:
            if protocol_chunk_size > MAX_ZMODEM_CHUNK_SIZE:
                raise ValueError(
                    f"chunk_size must be <= {MAX_ZMODEM_CHUNK_SIZE}")

        mesh_chunk_size = self.app_config.get("mesh_packet_chunk_size")
        if mesh_chunk_size is not None:
            if mesh_chunk_size < MIN_MESH_PACKET_CHUNK_SIZE:
                raise ValueError(
                    f"mesh_packet_chunk_size must be >= "
                    f"{MIN_MESH_PACKET_CHUNK_SIZE}")
            if mesh_chunk_size > MESHCORE_MAX_BINARY_CHUNK:
                logging.warning(
                    "mesh_packet_chunk_size %s exceeds the encoded MeshCore "
                    "limit %s; clamping",
                    mesh_chunk_size,
                    MESHCORE_MAX_BINARY_CHUNK)
                self.app_config["mesh_packet_chunk_size"] = (
                    MESHCORE_MAX_BINARY_CHUNK)

        allow_unsafe_tx_delay = self.app_config.get(
            "allow_unsafe_tx_delay", False)
        if not isinstance(allow_unsafe_tx_delay, bool):
            raise ValueError("allow_unsafe_tx_delay must be a boolean")
        auto_receive = self.app_config.get("auto_receive", True)
        if not isinstance(auto_receive, bool):
            raise ValueError("auto_receive must be a boolean")

        allowed = self.app_config.get("allowed_senders", [])
        if allowed is None:
            allowed = []
        if not isinstance(allowed, list) or not all(
                isinstance(item, str) for item in allowed):
            raise ValueError("allowed_senders must be a list of strings")

        tx_delay_ms = self.app_config.get(
            "tx_delay_ms", DEFAULT_CONFIG["tx_delay_ms"])
        min_tx_delay_ms = self.app_config.get(
            "min_tx_delay_ms", DEFAULT_CONFIG["min_tx_delay_ms"])
        if tx_delay_ms < min_tx_delay_ms and not allow_unsafe_tx_delay:
            raise ValueError(
                "tx_delay_ms must be >= min_tx_delay_ms unless "
                "allow_unsafe_tx_delay is true")

    def _transfer_summary(self, tid, transfer):
        now = time.time()
        total = transfer.get("total")
        byte_count = transfer.get("bytes", 0)
        progress = None
        if total:
            progress = min(1.0, byte_count / total)
        return {
            "id": tid,
            "state": transfer.get("state"),
            "file": transfer.get("file"),
            "dest": transfer.get("dest"),
            "bytes": byte_count,
            "total": total,
            "progress": progress,
            "started_at": transfer.get("start"),
            "last_activity": transfer.get("last_act"),
            "elapsed_seconds": now - transfer.get("start", now),
            "idle_seconds": now - transfer.get("last_act", now),
        }

    def get_status(self, tid=None):
        if tid is not None:
            transfer = self.transfers.get(tid)
            if not transfer:
                return {"ok": False, "error": f"transfer {tid} not found"}
            return {"ok": True, "transfer": self._transfer_summary(tid, transfer)}
        transfers = [
            self._transfer_summary(tid, transfer)
            for tid, transfer in sorted(self.transfers.items())
        ]
        return {"ok": True, "transfers": transfers}

    def _looks_like_zmodem_start(self, data):
        if len(data) < 7:
            return False
        frame_len = struct.unpack("!I", data[:4])[0]
        if frame_len > zmodem.MAX_FRAME_PAYLOAD:
            return False
        if data[4:5] != b"S":
            return False
        name_len = struct.unpack("!H", data[5:7])[0]
        return frame_len == 1 + 2 + name_len + 8

    async def start_control_server(self):
        host = self.app_config.get("control_host", DEFAULT_CONFIG["control_host"])
        port = self.app_config.get("control_port", DEFAULT_CONFIG["control_port"])
        try:
            self._control_server = await asyncio.start_server(
                self._handle_control_client,
                host,
                port,
            )
        except OSError as e:
            logging.warning(f"Control server unavailable on {host}:{port}: {e}")
            return False
        sockets = self._control_server.sockets or []
        bound = ", ".join(
            f"{sock.getsockname()[0]}:{sock.getsockname()[1]}"
            for sock in sockets)
        logging.info(f"Control server listening on {bound}")
        return True

    async def _handle_control_client(self, reader, writer):
        try:
            raw = await reader.readline()
            request = json.loads(raw.decode("utf-8"))
            command = request.get("command")
            tid = request.get("id")
            if tid is not None:
                tid = int(tid)

            if command == "status":
                response = self.get_status(tid)
            elif command == "cancel":
                if tid is None:
                    response = {
                        "ok": False,
                        "error": "cancel requires a transfer id"}
                else:
                    response = {"ok": self.cancel_transfer(tid)}
                    if not response["ok"]:
                        response["error"] = f"transfer {tid} not found"
            else:
                response = {"ok": False, "error": f"unknown command: {command}"}
        except Exception as e:
            response = {"ok": False, "error": str(e)}
        try:
            writer.write(
                (json.dumps(response, default=str) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

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
                self.mesh = await MeshCore.create_serial(
                    port, baudrate=baud, auto_reconnect=True)
            elif conn_type == "tcp":
                host = self.app_config.get("mesh_tcp_host")
                port = self.app_config.get("mesh_tcp_port")
                logging.info(f"Connecting TCP: {host}:{port}")
                self.mesh = await MeshCore.create_tcp(
                    host, port, auto_reconnect=True)

            if self.mesh:
                logging.info("Connected to MeshCore Network.")
                self.mesh.subscribe(
                    EventType.CONTACT_MSG_RECV,
                    self._on_mesh_message)
                self.mesh.subscribe(EventType.ERROR, self._on_mesh_error)
                start_fetch = getattr(
                    self.mesh, "start_auto_message_fetching", None)
                if callable(start_fetch):
                    await start_fetch()
                return True
        except Exception as e:
            logging.error(f"Connection Failed: {e}")
        return False

    async def _on_mesh_message(self, event):
        try:
            parsed = parse_inbound_event(event)
            if not parsed:
                return
            try:
                self._mesh_receive_queue.put_nowait(parsed)
            except asyncio.QueueFull:
                logging.warning(
                    "Inbound mesh queue full; dropping packet from %s",
                    parsed["source"])
        except Exception as e:
            logging.error(f"Msg Parse Error: {e}")

    async def _on_mesh_error(self, event):
        msg = event.payload if hasattr(event, 'payload') else event
        logging.warning(f"Mesh Error: {msg}")

    # -------------------------------------------------------------------------
    # Send Logic
    # -------------------------------------------------------------------------
    async def _mesh_send(self, dest, chunk: bytes):
        dest = resolve_destination(self.mesh, dest)
        message = encode_mesh_message(chunk)
        result = await self.mesh.commands.send_msg(dest, message)
        if is_command_error(result, EventType.ERROR):
            reason = getattr(result, "payload", result)
            raise RuntimeError(f"send_msg failed: {reason}")

    async def _send_packet_chunks(self, dest, packet: bytes):
        header = struct.pack(APP_PORT_HEADER_FORMAT, self.zmodem_app_port)
        remaining = packet
        max_payload = self.mesh_packet_chunk_size - len(header)
        if max_payload <= 0:
            raise ValueError("mesh_packet_chunk_size is too small")
        while remaining:
            piece = remaining[:max_payload]
            remaining = remaining[len(piece):]
            await self._mesh_send(dest, header + piece)
            await asyncio.sleep(self.tx_delay_s)

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
        if self.max_file_size_bytes and fsize > self.max_file_size_bytes:
            logging.error(
                f"File too large: {fsize:,} bytes exceeds max_file_size_bytes "
                f"({self.max_file_size_bytes:,})")
            if cli_event:
                cli_event.set()
            return None
        # calculate checksum in a thread to avoid blocking the event loop
        checksum = await asyncio.to_thread(calculate_md5, filepath)
        fname = os.path.basename(filepath)

        logging.info(
            f"[Tx-{tid}] File: {fname} | Size: {fsize:,} bytes | MD5: {checksum}")

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
            "file": filepath, "dest": dest_node,
            "dest_resolved": resolve_destination(self.mesh, dest_node),
            "start": time.time(),
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
            consecutive_failures = 0
            last_send = 0.0
            while self.running:
                if tid not in self.transfers:
                    break
                if await asyncio.to_thread(sender.is_finished):
                    logging.info(f"[Tx-{tid}] Transfer Complete.")
                    break

                packet = await asyncio.to_thread(sender.get_next_packet)
                retransmitting = False
                if not packet:
                    waiting = (
                        time.time() - last_send >= self.retransmit_timeout_s
                        and last_send > 0)
                    if waiting:
                        packet = await asyncio.to_thread(sender.peek_retransmit)
                        retransmitting = bool(packet)
                        if retransmitting:
                            logging.info(
                                f"[Tx-{tid}] Retransmitting unacked packet")

                if packet:
                    logging.debug(
                        f"[Tx-{tid}] next packet size {len(packet)} "
                        f"state={sender.state}")
                    try:
                        await self._send_packet_chunks(dest, packet)
                        consecutive_failures = 0
                        last_send = time.time()
                        t["last_act"] = last_send
                    except Exception as e:
                        consecutive_failures += 1
                        logging.warning(f"[Tx-{tid}] Send Fail: {e}")
                        if (
                                consecutive_failures
                                >= self.max_consecutive_send_failures):
                            logging.error(
                                f"[Tx-{tid}] Too many consecutive send "
                                "failures; cancelling transfer")
                            return
                        await asyncio.sleep(1.0)
                        continue

                    if pbar and not retransmitting:
                        pbar.update(getattr(sender, "last_payload_bytes", 0)
                                    or 0)
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
        if os.path.isdir(filepath):
            logging.error(f"Destination is a directory, not a file: {filepath}")
            if cli_event:
                cli_event.set()
            return None
        if os.path.exists(filepath) and not overwrite:
            logging.error(f"File exists: {filepath} (Use --overwrite)")
            if cli_event:
                cli_event.set()
            return None
        parent = os.path.dirname(os.path.abspath(filepath))
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                logging.error(f"Cannot create destination directory: {e}")
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

    def _incoming_size_allowed(self, data):
        parser = getattr(zmodem, "parse_start_header", None)
        if parser is None:
            return True
        info = parser(data)
        if info is None:
            return True
        _name, size = info
        if self.max_file_size_bytes and size > self.max_file_size_bytes:
            return False
        return True

    def _unique_incoming_path(self, filename):
        base = os.path.join(self.incoming_dir, filename)
        if not os.path.exists(base):
            return base
        stem, ext = os.path.splitext(filename)
        n = 1
        while True:
            candidate = os.path.join(self.incoming_dir, f"{stem}-{n}{ext}")
            if not os.path.exists(candidate):
                return candidate
            n += 1

    async def _match_transfer(self, src, data):
        for tid, t in list(self.transfers.items()):
            if t["state"] == "waiting":
                if not self._looks_like_zmodem_start(data):
                    logging.warning(
                        "[Rx-%s] Ignoring non-ZMODEM start from %s",
                        tid,
                        src)
                    return None
                if not self._incoming_size_allowed(data):
                    logging.error(
                        "[Rx-%s] Incoming file from %s exceeds "
                        "max_file_size_bytes",
                        tid,
                        src)
                    self.cancel_transfer(tid)
                    return None
                t["dest"] = src
                t["state"] = "receiving"
                t["sync_f"] = None
                try:
                    t["receiver"] = await asyncio.to_thread(
                        zmodem.Receiver, t["file"])
                except Exception as e:
                    logging.error(
                        f"[Rx-{tid}] Cannot init receiver for '{t['file']}': {e}")
                    self.cancel_transfer(tid)
                    continue
                logging.info(f"[Rx-{tid}] Incoming stream from {src} accepted")
                return tid
            if t["state"] == "receiving" and t.get("dest") == src:
                return tid
            if t["state"] == "sending" and (
                    t.get("dest") == src
                    or peer_matches(t.get("dest"), src)
                    or peer_matches(t.get("dest_resolved"), src)):
                return tid
        return None

    async def _maybe_auto_receive(self, src, data):
        if not self.auto_receive:
            return None
        if not self._looks_like_zmodem_start(data):
            return None
        if not sender_is_allowed(src, self.allowed_senders):
            logging.warning(
                "Ignoring inbound transfer from disallowed sender %s", src)
            return None
        if not self._incoming_size_allowed(data):
            logging.error(
                "Incoming file from %s exceeds max_file_size_bytes", src)
            return None
        parser = getattr(zmodem, "parse_start_header", None)
        info = parser(data) if parser else None
        raw_name = info[0] if info else None
        filename = (
            sanitize_filename(raw_name)
            or f"transfer-{self.transfer_id_counter + 1}")
        try:
            os.makedirs(self.incoming_dir, exist_ok=True)
        except OSError as e:
            logging.error("Cannot create incoming_dir '%s': %s",
                          self.incoming_dir, e)
            return None
        dest = self._unique_incoming_path(filename)
        logging.info("Auto-receive from %s -> %s", src, dest)
        return await self.receive_file(dest, overwrite=False)

    async def _handle_receiving(self, tid, t, data):
        receiver = t["receiver"]
        logging.debug(f"[Rx-{tid}] delivering {len(data)} bytes to receiver")
        resp = await asyncio.to_thread(receiver.receive, data)
        t["bytes"] += len(data)
        expected = getattr(receiver, "expected_size", None)
        if (self.max_file_size_bytes and expected
                and expected > self.max_file_size_bytes):
            logging.error(
                f"[Rx-{tid}] Advertised size {expected} exceeds "
                "max_file_size_bytes")
            self.cancel_transfer(tid)
            return
        if resp:
            logging.debug(f"[Rx-{tid}] sending {len(resp)} bytes back")
            await self._send_packet_chunks(t["dest"], resp)
        if await asyncio.to_thread(receiver.is_finished):
            logging.info(f"[Rx-{tid}] Transfer Complete.")
            checksum = await asyncio.to_thread(calculate_md5, t["file"])
            logging.info(f"[Rx-{tid}] File Saved. MD5: {checksum}")
            self.cancel_transfer(tid)

    async def _handle_sending_ack(self, tid, t, data):
        logging.debug(f"[Tx-{tid}] delivering {len(data)} bytes to sender")
        resp = await asyncio.to_thread(t["sender"].receive, data)
        logging.debug(
            f"[Tx-{tid}] sender returned {len(resp) if resp else 0} bytes")
        if resp:
            await self._send_packet_chunks(t["dest"], resp)

    async def _handle_zmodem_data(self, src, data):
        active_tid = await self._match_transfer(src, data)
        if not active_tid:
            created = await self._maybe_auto_receive(src, data)
            if created:
                active_tid = await self._match_transfer(src, data)
        if not active_tid:
            return
        t = self.transfers[active_tid]
        t["last_act"] = time.time()

        try:
            if t["state"] == "receiving":
                await self._handle_receiving(active_tid, t, data)
            elif t["state"] == "sending" and t.get("sender"):
                await self._handle_sending_ack(active_tid, t, data)
        except Exception as e:
            logging.error(f"[Tx/Rx-{active_tid}] Protocol error: {e}")
            if t.get("state") == "receiving":
                self.cancel_transfer(active_tid)
    # -------------------------------------------------------------------------
    # Directory Handling & Management
    # -------------------------------------------------------------------------

    async def send_directory(self, dest, path, cli_event, cleanup=True):
        if not self.mesh:
            if cli_event:
                cli_event.set()
            return None
        if not isinstance(dest, str) or not dest:
            logging.error(f"Invalid destination node: {dest}")
            if cli_event:
                cli_event.set()
            return None
        if not os.path.isdir(path):
            logging.error(f"Directory not found: {path}")
            if cli_event:
                cli_event.set()
            return None

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
        if os.path.exists(path) and not os.path.isdir(path):
            logging.error(f"Directory destination is a file: {path}")
            if cli_event:
                cli_event.set()
            return None
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        # create a secure temporary file for the incoming zip to avoid
        # predictable filenames and TOCTOU issues
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            zip_name = tf.name

        f_event = asyncio.Event()
        tid = await self.receive_file(zip_name, True, f_event)
        if tid:
            self._temp_zips.append(zip_name)
            self._register_temp(zip_name)
            await f_event.wait()
            if os.path.exists(zip_name):
                logging.info(f"Extracting to '{path}'...")
                try:
                    await asyncio.to_thread(
                        _safe_extract_zip,
                        zip_name,
                        path,
                        overwrite,
                        self.max_file_size_bytes or None)
                except UnsafeZipError as e:
                    logging.error(f"Received zip rejected as unsafe: {e}")
                except Exception as e:
                    logging.error(
                        f"Failed to extract received zip '{zip_name}': {e}")
                finally:
                    if cleanup:
                        try:
                            os.remove(zip_name)
                        except OSError:
                            pass
        elif cleanup:
            try:
                os.remove(zip_name)
            except OSError:
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
        if self._control_server:
            self._control_server.close()
            try:
                await self._control_server.wait_closed()
            except Exception:
                pass
            self._control_server = None
        for tid in list(self.transfers.keys()):
            self.cancel_transfer(tid)
        if self.mesh:
            try:
                if hasattr(self.mesh, "disconnect"):
                    await self.mesh.disconnect()
                elif hasattr(self.mesh, "close"):
                    await self.mesh.close()
            except Exception:
                pass

# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------


async def send_control_command(config, command, transfer_id=None):
    host = config.get("control_host", DEFAULT_CONFIG["control_host"])
    port = config.get("control_port", DEFAULT_CONFIG["control_port"])
    request = {"command": command}
    if transfer_id is not None:
        request["id"] = transfer_id
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write((json.dumps(request) + "\n").encode("utf-8"))
        await writer.drain()
        raw = await reader.readline()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if not raw:
            return {"ok": False, "error": "control server closed without a response"}
        return json.loads(raw.decode("utf-8"))
    except OSError as e:
        return {"ok": False, "error": f"could not reach control server at {host}:{port}: {e}"}


async def main():
    parser = argparse.ArgumentParser(description="Akita-Zmodem-MeshCore")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}")
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
    parser.add_argument("--control-host", dest="control_host",
                        help="Local control host for status/cancel commands")
    parser.add_argument("--control-port", dest="control_port", type=int,
                        help="Local control port for status/cancel commands")

    sub = parser.add_subparsers(dest="command")

    p_send = sub.add_parser("send")
    p_send.add_argument("dest", help="Dest Node ID")
    p_send.add_argument("path", help="File/Dir path")

    p_recv = sub.add_parser("receive")
    p_recv.add_argument("path", help="Save path (directory or filename)")
    p_recv.add_argument("--overwrite", action="store_true")
    p_recv.add_argument("--directory", action="store_true",
                        help="Force treat the destination as a directory")

    sub.add_parser("status").add_argument("id", type=int, nargs="?")
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
    if args.control_host:
        overrides["control_host"] = args.control_host
    if args.control_port is not None:
        overrides["control_port"] = args.control_port

    app = AkitaZmodemMeshCore(overrides, config_file=args.config)

    if args.command in ("status", "cancel"):
        response = await send_control_command(
            app.app_config,
            args.command,
            getattr(args, "id", None),
        )
        print(json.dumps(response, default=str, indent=2))
        return

    # Clean Exit
    def sig_handler():
        logging.info("Interrupted. Stopping...")
        asyncio.create_task(app.stop())
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, sig_handler)
    except (NotImplementedError, RuntimeError):
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
    await app.start_control_server()

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

        else:
            if app.auto_receive:
                try:
                    os.makedirs(app.incoming_dir, exist_ok=True)
                except OSError as e:
                    logging.error(
                        "Cannot create incoming_dir '%s': %s",
                        app.incoming_dir,
                        e)
                logging.info(
                    "Daemon listening. Auto-receive directory: %s "
                    "(Ctrl+C to stop)",
                    os.path.abspath(app.incoming_dir))
            else:
                logging.info(
                    "Daemon listening without auto-receive. "
                    "Pre-declare a path with the receive command. "
                    "(Ctrl+C to stop)")
            while app.running:
                await asyncio.sleep(1)

    except asyncio.CancelledError:
        pass
    finally:
        await app.stop()


def main_entry():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main_entry()
