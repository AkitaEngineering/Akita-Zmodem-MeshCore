#!/usr/bin/env python3
import asyncio
import logging
import os
import zipfile
import io
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

check_dependency("meshcore", "meshcore")
check_dependency("zmodem", "zmodem")

from meshcore import MeshCore, EventType
import zmodem

# Optional: TQDM for progress bars
try:
    from tqdm.asyncio import tqdm
    TQDM_AVAILABLE = True
except Exception:
    TQDM_AVAILABLE = False
    try:
        print("Suggestion: Install 'tqdm' for progress bars (pip install tqdm)", file=sys.stderr)
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------
CONFIG_FILE = "akita_zmodem_meshcore_config.json"
DEFAULT_CONFIG = {
    "zmodem_app_port": 2001,
    "chunk_size": 256,             # Internal Zmodem buffer size
    "mesh_packet_chunk_size": 200, # Max payload per mesh packet (LoRa MTU safe)
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
def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            loaded = json.load(f)
            config = DEFAULT_CONFIG.copy()
            config.update(loaded)
            return config
    except (FileNotFoundError, json.JSONDecodeError):
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG

config_data = load_config()
ZMODEM_APP_PORT = config_data["zmodem_app_port"]
MESH_PACKET_CHUNK_SIZE = config_data["mesh_packet_chunk_size"]
TIMEOUT = config_data["timeout"]
TX_DELAY_S = config_data.get("tx_delay_ms", 150) / 1000.0

def calculate_md5(filepath):
    """Calculates MD5 checksum of a file for integrity verification."""
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

# -----------------------------------------------------------------------------
# Core Class
# -----------------------------------------------------------------------------
class AkitaZmodemMeshCore:
    def __init__(self, cli_config_overrides=None):
        self.mesh = None
        self.transfers = {}
        self.transfer_id_counter = 0
        self.running = True
        self._mesh_receive_queue = asyncio.Queue()
        self.app_config = config_data.copy()
        if cli_config_overrides:
            self.app_config.update(cli_config_overrides)

    def generate_transfer_id(self):
        self.transfer_id_counter += 1
        return self.transfer_id_counter

    async def _connect_mesh(self):
        conn_type = self.app_config.get("mesh_connection_type", "serial")
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
                self.mesh.subscribe(EventType.CONTACT_MSG_RECV, self._on_mesh_message)
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
                if isinstance(txt, str): data = txt.encode('utf-8', 'ignore')
                elif isinstance(txt, bytes): data = txt

            if data and isinstance(data, bytes):
                await self._mesh_receive_queue.put({"source": src, "data": data})
        except Exception as e:
            logging.error(f"Msg Parse Error: {e}")

    async def _on_mesh_error(self, event):
        logging.warning(f"Mesh Error: {event.payload if hasattr(event, 'payload') else event}")

    # -------------------------------------------------------------------------
    # Send Logic
    # -------------------------------------------------------------------------
    async def send_file(self, dest_node, filepath, cli_event=None):
        if not self.mesh:
            if cli_event: cli_event.set()
            return None

        if not os.path.exists(filepath):
            logging.error(f"File not found: {filepath}")
            if cli_event: cli_event.set()
            return None

        tid = self.generate_transfer_id()
        fsize = os.path.getsize(filepath)
        checksum = calculate_md5(filepath)
        
        logging.info(f"[Tx-{tid}] File: {os.path.basename(filepath)} | Size: {fsize:,} bytes | MD5: {checksum}")

        try:
            # Open file in thread to avoid blocking loop
            sync_f = await asyncio.to_thread(open, filepath, "rb")
            sender = await asyncio.to_thread(zmodem.Sender, sync_f)
        except Exception as e:
            logging.error(f"Zmodem Init Error: {e}")
            if 'sync_f' in locals() and sync_f: sync_f.close()
            if cli_event: cli_event.set()
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
            pbar = tqdm(total=t["total"], desc=f"Tx-{tid}", unit="B", unit_scale=True, leave=True)

        try:
            while self.running:
                if await asyncio.to_thread(sender.is_finished):
                    logging.info(f"[Tx-{tid}] Transfer Complete.")
                    break

                # Get packet from Zmodem
                packet = await asyncio.to_thread(sender.get_next_packet)

                if packet:
                    # Construct Payload: [PORT HEADER] + [ZMODEM DATA]
                    full_payload = struct.pack(APP_PORT_HEADER_FORMAT, ZMODEM_APP_PORT) + packet
                    
                    # Chunking
                    chunk_len = len(full_payload)
                    for i in range(0, chunk_len, MESH_PACKET_CHUNK_SIZE):
                        chunk = full_payload[i:i + MESH_PACKET_CHUNK_SIZE]
                        try:
                            await self.mesh.commands.send_msg(destination=dest, payload=chunk)
                            t["last_act"] = time.time()
                            # Throttle
                            await asyncio.sleep(TX_DELAY_S) 
                        except Exception as e:
                            logging.warning(f"[Tx-{tid}] Send Fail: {e}")
                            await asyncio.sleep(1.0) # Backoff
                    
                    if pbar: pbar.update(len(packet))
                else:
                    await asyncio.sleep(0.1)

        except Exception as e:
            logging.error(f"[Tx-{tid}] Error: {e}")
        finally:
            if pbar: pbar.close()
            self.cancel_transfer(tid)

    # -------------------------------------------------------------------------
    # Receive Logic
    # -------------------------------------------------------------------------
    async def receive_file(self, filepath, overwrite=False, cli_event=None):
        if os.path.exists(filepath) and not overwrite:
            logging.error(f"File exists: {filepath} (Use --overwrite)")
            if cli_event: cli_event.set()
            return None

        tid = self.generate_transfer_id()
        self.transfers[tid] = {
            "state": "waiting", "receiver": None, "sync_f": None,
            "file": filepath, "dest": None, "start": time.time(),
            "last_act": time.time(), "buffer": b"", "bytes": 0,
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

                if len(data) <= APP_PORT_HEADER_SIZE: continue

                port = struct.unpack(APP_PORT_HEADER_FORMAT, data[:APP_PORT_HEADER_SIZE])[0]
                if port == ZMODEM_APP_PORT:
                    await self._handle_zmodem_data(src, data[APP_PORT_HEADER_SIZE:])
                
            except asyncio.TimeoutError: pass
            except Exception as e: logging.error(f"Listener Error: {e}")

    async def _handle_zmodem_data(self, src, data):
        active_tid = None
        
        # Match incoming packet to transfer
        for tid, t in self.transfers.items():
            if t["state"] == "waiting":
                t["dest"] = src
                t["state"] = "receiving"
                t["sync_f"] = await asyncio.to_thread(open, t["file"], "wb")
                t["receiver"] = await asyncio.to_thread(zmodem.Receiver, t["sync_f"])
                logging.info(f"[Rx-{tid}] Incoming stream from {src} accepted")
                active_tid = tid
                break
            elif t["state"] == "receiving" and t["dest"] == src:
                active_tid = tid
                break
        
        if not active_tid: return

        t = self.transfers[active_tid]
        t["last_act"] = time.time()
        t["buffer"] += data
        receiver = t["receiver"]

        try:
            # Feed Zmodem Receiver
            await asyncio.to_thread(receiver.receive, t["buffer"])
            t["bytes"] += len(t["buffer"])
            t["buffer"] = b"" # Flush

            if await asyncio.to_thread(receiver.is_finished):
                logging.info(f"[Rx-{active_tid}] Transfer Complete.")
                checksum = calculate_md5(t["file"])
                logging.info(f"[Rx-{active_tid}] File Saved. MD5: {checksum}")
                self.cancel_transfer(active_tid)
        except Exception as e:
            logging.error(f"[Rx-{active_tid}] Zmodem Protocol Error: {e}")
            self.cancel_transfer(active_tid)

    # -------------------------------------------------------------------------
    # Directory Handling & Management
    # -------------------------------------------------------------------------
    async def send_directory(self, dest, path, cli_event):
        zip_name = os.path.abspath(f"akita_{os.path.basename(path)}_{int(time.time())}.zip")
        logging.info(f"Compressing directory '{path}'...")

        def _zip():
            with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(path):
                    for file in files:
                        p = os.path.join(root, file)
                        z.write(p, os.path.relpath(p, path))

        await asyncio.to_thread(_zip)

        f_event = asyncio.Event()
        tid = await self.send_file(dest, zip_name, f_event)

        try:
            if tid:
                await f_event.wait()
        finally:
            # Ensure the temporary zip is removed where possible
            try:
                if os.path.exists(zip_name):
                    os.remove(zip_name)
            except Exception as e:
                logging.warning(f"Failed to remove temp zip '{zip_name}': {e}")
            if cli_event:
                try: cli_event.set()
                except Exception: pass

    async def receive_directory(self, path, overwrite, cli_event):
        if not os.path.exists(path): os.makedirs(path)
        zip_name = f"akita_recv_{int(time.time())}.zip"
        
        f_event = asyncio.Event()
        tid = await self.receive_file(zip_name, overwrite, f_event)
        
        if tid:
            await f_event.wait()
            if os.path.exists(zip_name):
                logging.info(f"Extracting to '{path}'...")
                await asyncio.to_thread(lambda: zipfile.ZipFile(zip_name, 'r').extractall(path))
                os.remove(zip_name)
        if cli_event: cli_event.set()

    def cancel_transfer(self, tid):
        if tid in self.transfers:
            t = self.transfers.pop(tid)
            if t.get("sync_f"): 
                try: t["sync_f"].close()
                except: pass
            if t.get("cli_event"): 
                t["cli_event"].set()
            return True
        return False

    async def _timeout_check(self):
        while self.running:
            now = time.time()
            for tid in list(self.transfers.keys()):
                if now - self.transfers[tid]["last_act"] > TIMEOUT:
                    logging.warning(f"[Tx/Rx-{tid}] Timeout. Cancelling.")
                    self.cancel_transfer(tid)
            await asyncio.sleep(5)

    async def stop(self):
        self.running = False
        for tid in list(self.transfers.keys()): self.cancel_transfer(tid)
        if self.mesh: 
            try: await self.mesh.close()
            except: pass

# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="Akita-Zmodem-MeshCore")
    parser.add_argument("--config", default=CONFIG_FILE)
    parser.add_argument("--mesh-type", choices=["serial", "tcp"])
    parser.add_argument("--serial-port")
    parser.add_argument("--serial-baud", type=int)
    
    sub = parser.add_subparsers(dest="command")
    
    p_send = sub.add_parser("send")
    p_send.add_argument("dest", help="Dest Node ID")
    p_send.add_argument("path", help="File/Dir path")
    
    p_recv = sub.add_parser("receive")
    p_recv.add_argument("path", help="Save path")
    p_recv.add_argument("--overwrite", action="store_true")

    sub.add_parser("status").add_argument("id", type=int)
    sub.add_parser("cancel").add_argument("id", type=int)

    args = parser.parse_args()

    overrides = {}
    if args.mesh_type: overrides["mesh_connection_type"] = args.mesh_type
    if args.serial_port: overrides["mesh_serial_port"] = args.serial_port
    if args.serial_baud: overrides["mesh_serial_baud"] = args.serial_baud

    app = AkitaZmodemMeshCore(overrides)

    # Clean Exit
    def sig_handler():
        logging.info("Interrupted. Stopping...")
        asyncio.create_task(app.stop())
    try: asyncio.get_running_loop().add_signal_handler(signal.SIGINT, sig_handler)
    except: pass

    if not await app._connect_mesh(): return

    cli_event = asyncio.Event()

    try:
        if args.command == "send":
            if os.path.isdir(args.path):
                await app.send_directory(args.dest, args.path, cli_event)
            else:
                await app.send_file(args.dest, args.path, cli_event)
            await cli_event.wait()

        elif args.command == "receive":
            # Auto-detect directory intention by path extension (or lack thereof)
            is_dir = not args.path.endswith(('.zip','.bin','.txt','.dat','.jpg','.png','.py','.json'))
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
            # Daemon
            asyncio.create_task(app._receive_loop_processor())
            asyncio.create_task(app._timeout_check())
            logging.info(f"Daemon Listening. ID: {app.mesh} (Ctrl+C to stop)")
            while app.running: await asyncio.sleep(1)

    except asyncio.CancelledError: pass
    finally: await app.stop()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
