import asyncio
import logging
import os
import zipfile
import io
import argparse
import json
import time
import struct # Added for port handling in payload

# Attempt to import meshcore components assuming fdlamotte/meshcore_py
try:
    from meshcore import MeshCore, EventType # For fdlamotte/meshcore_py
except ImportError:
    # Provide a mock for environments where meshcore isn't installed or is different
    # This allows understanding the structure, but it won't run.
    logging.warning("meshcore library not found or incompatible. Using mock objects. Functionality will be limited.")
    class MockMeshCore:
        def __init__(self, *args, **kwargs): pass
        async def create_serial(self, device, baud): return self
        async def create_tcp(self, host, port): return self
        def subscribe(self, event_type, callback): pass
        async def close(self): pass
        class MockCommands:
            async def send_msg(self, destination, payload):
                logging.info(f"MOCK MESH: send_msg to {destination} with payload (first 20 bytes): {payload[:20]}")
                return True # Simulate success
        commands = MockCommands()

    class MockEventType:
        CONTACT_MSG_RECV = "CONTACT_MSG_RECV"
        ERROR = "ERROR" # Example
        # Add other event types if your meshcore lib has more that you might subscribe to

    MeshCore = MockMeshCore
    EventType = MockEventType


# Assuming zmodem library is installed via 'pip install zmodem'
# If its API is different, these parts would need adjustment.
try:
    import zmodem
except ImportError:
    logging.warning("zmodem library not found. Using mock objects. Functionality will be limited.")
    class MockZmodemSender:
        def __init__(self, stream):
            self.stream = stream
            self._finished = False
            self._packets_sent = 0
            self.file_pos = 0
            if hasattr(self.stream, 'seek') and hasattr(self.stream, 'tell'):
                self.stream.seek(0, os.SEEK_END)
                self.file_len = self.stream.tell()
                self.stream.seek(0, os.SEEK_SET)
            else: # In-memory streams might not have seek/tell in the same way, or it's a BytesIO
                try:
                    self.file_len = len(self.stream.getvalue())
                except AttributeError:
                    self.file_len = 0 # Cannot determine length

        def get_next_packet(self):
            if self.file_pos < self.file_len:
                chunk_size = 128 # Arbitrary chunk for mock
                remaining = self.file_len - self.file_pos
                read_size = min(chunk_size, remaining)
                if hasattr(self.stream, 'read'):
                    data = self.stream.read(read_size)
                    if data:
                        self.file_pos += len(data)
                        self._packets_sent += 1
                        return data
            self._finished = True
            return None

        def is_finished(self): return self._finished
        def cancel(self): # Mock cancel
            self._finished = True
            logging.info("MOCK ZMODEM: Sender cancelled.")


    class MockZmodemReceiver:
        def __init__(self, stream):
            self.stream = stream
            self._finished = False
            self._packets_received = 0
            self.expected_data_amount = 512 # Arbitrary for mock, real zmodem determines this
            self.received_so_far = 0


        def receive(self, data): # Simulate consuming data
            if data and hasattr(self.stream, 'write'):
                self.stream.write(data)
                self.received_so_far += len(data)
                self._packets_received +=1
                if self.received_so_far >= self.expected_data_amount: # Simulate end of file based on expected amount
                    self._finished = True
                return None # Or remaining data if partially processed
            return data # Return data if not processed

        def is_finished(self): return self._finished
        def cancel(self): # Mock cancel
             self._finished = True
             logging.info("MOCK ZMODEM: Receiver cancelled.")


    zmodem = type('zmodem', (object,), {'Sender': MockZmodemSender, 'Receiver': MockZmodemReceiver})()


import aiofiles # Added for async file operations

# Configuration
CONFIG_FILE = "akita_zmodem_meshcore_config.json"
DEFAULT_CONFIG = {
    "zmodem_app_port": 2001, # Renamed to avoid conflict if meshcore has a 'port'
    "chunk_size": 256,      # Zmodem internal chunking might differ from mesh packet chunking
    "mesh_packet_chunk_size": 128, # Max size for a single mesh packet payload
    "timeout": 60, # Increased default timeout
    "mesh_connection_type": "serial", # "serial" or "tcp"
    "mesh_serial_port": "/dev/ttyUSB0",
    "mesh_serial_baud": 115200,
    "mesh_tcp_host": "127.0.0.1",
    "mesh_tcp_port": 4403
}
APP_PORT_HEADER_FORMAT = "!H" # 2 bytes for port number
APP_PORT_HEADER_SIZE = struct.calcsize(APP_PORT_HEADER_FORMAT)

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            loaded_config = json.load(f)
            # Ensure all default keys are present by merging
            config = DEFAULT_CONFIG.copy()
            config.update(loaded_config) # loaded_config overrides defaults
            return config
    except FileNotFoundError:
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG
    except json.JSONDecodeError:
        logging.error(f"Invalid configuration file '{CONFIG_FILE}'. Using default settings and overwriting.")
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG

config_data = load_config() # Load config once globally
ZMODEM_APP_PORT = config_data["zmodem_app_port"]
MESH_PACKET_CHUNK_SIZE = config_data["mesh_packet_chunk_size"]
TIMEOUT = config_data["timeout"]

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class AkitaZmodemMeshCore:
    def __init__(self, cli_config_overrides=None): # Accept overrides
        self.mesh = None
        self.transfers = {}
        self.transfer_id_counter = 0
        self.running = True
        self._mesh_receive_queue = asyncio.Queue()
        self._transfer_completion_events = {} # For receive_directory and CLI send/receive
        self.app_config = config_data.copy() # Start with global config
        if cli_config_overrides:
            self.app_config.update(cli_config_overrides) # Apply CLI overrides


    def generate_transfer_id(self):
        self.transfer_id_counter += 1
        return self.transfer_id_counter

    async def _connect_mesh(self):
        conn_type = self.app_config.get("mesh_connection_type", "serial")
        try:
            if conn_type == "serial":
                port = self.app_config.get("mesh_serial_port")
                baud = self.app_config.get("mesh_serial_baud")
                logging.info(f"Attempting to connect to MeshCore via Serial: {port}@{baud}")
                self.mesh = await MeshCore.create_serial(device=port, baud=baud)
            elif conn_type == "tcp":
                host = self.app_config.get("mesh_tcp_host")
                tcp_port = self.app_config.get("mesh_tcp_port")
                logging.info(f"Attempting to connect to MeshCore via TCP: {host}:{tcp_port}")
                self.mesh = await MeshCore.create_tcp(host=host, port=tcp_port)
            else:
                logging.error(f"Unsupported mesh_connection_type: {conn_type}")
                return False

            if self.mesh:
                logging.info("Successfully connected to MeshCore.")
                self.mesh.subscribe(EventType.CONTACT_MSG_RECV, self._on_mesh_message)
                self.mesh.subscribe(EventType.ERROR, self._on_mesh_error)
                return True
            else:
                logging.error("MeshCore connection object not created.") # Should be caught by exception
                return False
        except Exception as e:
            logging.error(f"Failed to connect to MeshCore ({conn_type}): {e}")
            self.mesh = None
            return False

    async def _on_mesh_message(self, event):
        try:
            payload_data = event.payload
            source_address_str = str(payload_data.get('from_num', payload_data.get('from', 'unknown_source'))) # MeshCore uses 'from' or 'from_num'
            data_bytes = payload_data.get('decoded', {}).get('payload') # MeshCore often has {'decoded': {'payload': b'...'}}

            if not data_bytes or not isinstance(data_bytes, bytes):
                # Try 'text' as fallback if 'decoded.payload' isn't there or not bytes
                data_bytes_alt = payload_data.get('text')
                if isinstance(data_bytes_alt, str): data_bytes = data_bytes_alt.encode('utf-8', 'ignore')
                elif isinstance(data_bytes_alt, bytes): data_bytes = data_bytes_alt

            if not data_bytes or not isinstance(data_bytes, bytes):
                logging.warning(f"Received mesh message with no valid data payload: {payload_data}")
                return

            await self._mesh_receive_queue.put({"source": source_address_str, "data": data_bytes})
        except Exception as e:
            logging.error(f"Error processing incoming mesh message: {e} - Event: {event}", exc_info=True)

    async def _on_mesh_error(self, event):
        logging.error(f"MeshCore Error Event: {event.payload if hasattr(event, 'payload') else event}")


    async def send_file(self, mesh_core_address_str, filepath, cli_wait_event=None):
        if not self.mesh:
            logging.error("Mesh not connected. Cannot send file.")
            if cli_wait_event: cli_wait_event.set() # Signal failure
            return None
        transfer_id = self.generate_transfer_id()
        try:
            file_size = await asyncio.to_thread(os.path.getsize, filepath)
        except OSError as e:
            logging.error(f"Cannot get size of file '{filepath}': {e}")
            if cli_wait_event: cli_wait_event.set()
            return None

        sync_f = None
        try:
            sync_f = await asyncio.to_thread(open, filepath, "rb")
            sender = await asyncio.to_thread(zmodem.Sender, sync_f)
        except Exception as e:
            logging.error(f"Failed to initialize zmodem.Sender for {filepath}: {e}")
            if sync_f: await asyncio.to_thread(sync_f.close)
            if cli_wait_event: cli_wait_event.set()
            return None

        self.transfers[transfer_id] = {
            "state": "sending", "sender": sender, "sync_file_handle": sync_f,
            "file": filepath, "address": mesh_core_address_str,
            "start_time": time.time(), "last_activity": time.time(),
            "bytes_sent_payload": 0, "total_bytes": file_size,
            "cli_wait_event": cli_wait_event # Store event to set on completion/failure
        }
        logging.info(f"Transfer {transfer_id}: Sending file '{filepath}' ({file_size} bytes) to {mesh_core_address_str}")
        asyncio.create_task(self._send_loop_task(transfer_id))
        return transfer_id

    async def _send_loop_task(self, transfer_id):
        if transfer_id not in self.transfers:
            logging.error(f"Transfer {transfer_id} not found for sending.")
            return

        transfer = self.transfers[transfer_id]
        sender = transfer["sender"]
        address_str = transfer["address"]
        sync_f = transfer["sync_file_handle"]
        cli_wait_event = transfer.get("cli_wait_event")
        success = False

        try:
            while self.running and not await asyncio.to_thread(sender.is_finished):
                if not self.mesh:
                    logging.warning(f"Transfer {transfer_id}: Mesh disconnected. Pausing send.")
                    await asyncio.sleep(5)
                    continue
                try:
                    zmodem_packet_data = await asyncio.to_thread(sender.get_next_packet)
                    if zmodem_packet_data:
                        data_to_send_on_mesh = struct.pack(APP_PORT_HEADER_FORMAT, ZMODEM_APP_PORT) + zmodem_packet_data
                        for i in range(0, len(data_to_send_on_mesh), MESH_PACKET_CHUNK_SIZE):
                            chunk = data_to_send_on_mesh[i:i + MESH_PACKET_CHUNK_SIZE]
                            try:
                                # MeshCore send_msg expects destination (nodeId) and payload (bytes)
                                # Ensure address_str is the correct format (e.g., !<node_id_hex>)
                                await self.mesh.commands.send_msg(destination=address_str, payload=chunk)
                                transfer["bytes_sent_payload"] += len(zmodem_packet_data) if i == 0 else 0 # Count ZMODEM payload once
                                transfer["last_activity"] = time.time()
                                await asyncio.sleep(0.02) # Increased slightly for better yield
                            except Exception as mesh_send_err:
                                logging.error(f"Transfer {transfer_id}: Mesh send error: {mesh_send_err}. Retrying soon.")
                                await asyncio.sleep(2)
                    else:
                        if await asyncio.to_thread(sender.is_finished): break
                        await asyncio.sleep(0.1)
                except Exception as e:
                    logging.error(f"Transfer {transfer_id}: Error in send loop: {e}", exc_info=True)
                    self.cancel_transfer(transfer_id) # This will also set cli_wait_event
                    return
            if await asyncio.to_thread(sender.is_finished):
                logging.info(f"Transfer {transfer_id}: File '{transfer['file']}' sent to {address_str}")
                success = True
            else:
                logging.warning(f"Transfer {transfer_id}: Send loop exited but Zmodem sender not finished (running: {self.running}).")
        finally:
            if sync_f: await asyncio.to_thread(sync_f.close)
            if transfer_id in self.transfers: # If not already cancelled
                if success: del self.transfers[transfer_id]
                else: self.cancel_transfer(transfer_id) # Ensure cleanup if failed/incomplete
            if cli_wait_event and not cli_wait_event.is_set(): # Check if not already set by cancel_transfer
                cli_wait_event.set()


    async def receive_file(self, filepath, overwrite=False, cli_wait_event=None):
        if await asyncio.to_thread(os.path.exists, filepath) and not overwrite:
            logging.error(f"File '{filepath}' already exists. Use --overwrite to overwrite.")
            if cli_wait_event: cli_wait_event.set() # Signal failure
            return None

        transfer_id = self.generate_transfer_id()
        if cli_wait_event: self._transfer_completion_events[transfer_id] = cli_wait_event
        # For general daemon mode, cli_wait_event will be None

        self.transfers[transfer_id] = {
            "state": "receiving_chờ", "receiver": None, "sync_file_handle": None,
            "file": filepath, "address": None, "start_time": time.time(),
            "last_activity": time.time(), "received_data_buffer": b"",
            "bytes_received_payload": 0, "cli_wait_event": cli_wait_event
        }
        logging.info(f"Transfer {transfer_id}: Prepared to receive file to '{filepath}'")
        return transfer_id

    async def _receive_loop_processor(self):
        logging.info("Receive loop processor started.")
        while self.running:
            if not self.mesh and self.running:
                logging.warning("Mesh not connected in receive loop. Waiting...")
                await asyncio.sleep(5)
                if not self.running: break
                if not await self._connect_mesh(): await asyncio.sleep(10)
                continue
            try:
                item = await asyncio.wait_for(self._mesh_receive_queue.get(), timeout=1.0)
                source_address = item["source"]
                data = item["data"]

                if len(data) < APP_PORT_HEADER_SIZE:
                    logging.warning(f"Rcvd packet from {source_address} too short for port: {len(data)} bytes")
                    continue
                app_port = struct.unpack(APP_PORT_HEADER_FORMAT, data[:APP_PORT_HEADER_SIZE])[0]
                actual_payload = data[APP_PORT_HEADER_SIZE:]

                if app_port == ZMODEM_APP_PORT:
                    await self._process_received_zmodem_payload(source_address, actual_payload)
                else:
                    logging.debug(f"Rcvd packet from {source_address} for unhandled app_port {app_port}")
            except asyncio.TimeoutError: continue
            except Exception as e:
                logging.error(f"Error in receive loop: {e}", exc_info=True)
                await asyncio.sleep(1)
        logging.info("Receive loop processor stopped.")

    async def _process_received_zmodem_payload(self, source_address, payload_chunk):
        active_receive_transfer_id = None
        active_receive_transfer = None
        for tid, tinfo in self.transfers.items():
            if tinfo["state"] in ["receiving_chờ", "receiving"]:
                if tinfo["address"] is None:
                    tinfo["address"] = source_address # Assign first incoming source to waiting transfer
                    active_receive_transfer_id = tid
                    active_receive_transfer = tinfo
                    logging.info(f"Transfer {tid}: First packet from {source_address}, assigned to this transfer for '{tinfo['file']}'.")
                    break
                elif tinfo["address"] == source_address: # Existing transfer from this source
                    active_receive_transfer_id = tid
                    active_receive_transfer = tinfo
                    break
        if not active_receive_transfer:
            logging.debug(f"No active/waiting ZMODEM transfer found or matched for source {source_address}. Discarding packet.")
            return

        transfer_id = active_receive_transfer_id
        transfer = active_receive_transfer

        if transfer["state"] == "receiving_chờ":
            transfer["state"] = "receiving"
            sync_f_write = None
            try:
                sync_f_write = await asyncio.to_thread(open, transfer["file"], "wb")
                transfer["sync_file_handle"] = sync_f_write
                transfer["receiver"] = await asyncio.to_thread(zmodem.Receiver, sync_f_write)
                logging.info(f"Transfer {transfer_id}: Receiver initialized for {transfer['file']} from {source_address}.")
            except Exception as e:
                logging.error(f"Transfer {transfer_id}: Failed to initialize zmodem.Receiver for {transfer['file']}: {e}")
                if sync_f_write: await asyncio.to_thread(sync_f_write.close) # Ensure closure on init error
                self.cancel_transfer(transfer_id)
                return

        transfer["received_data_buffer"] += payload_chunk
        transfer["last_activity"] = time.time()
        receiver = transfer["receiver"]
        success = False

        while len(transfer["received_data_buffer"]) > 0 and not await asyncio.to_thread(receiver.is_finished):
            try:
                # Pass a copy or manage consumption if receiver doesn't consume all
                # Assuming receiver.receive processes what it can from the buffer passed.
                # For this mock, it's assumed to consume the whole buffer if not finished.
                data_to_process = transfer["received_data_buffer"]
                transfer["received_data_buffer"] = b"" # Assume consumed or will be set to remainder by lib

                await asyncio.to_thread(receiver.receive, data_to_process)
                transfer["bytes_received_payload"] += len(data_to_process)

                if await asyncio.to_thread(receiver.is_finished): break
            except Exception as e:
                logging.error(f"Transfer {transfer_id}: Zmodem receive error: {e}", exc_info=True)
                self.cancel_transfer(transfer_id)
                return

        if await asyncio.to_thread(receiver.is_finished):
            logging.info(f"Transfer {transfer_id}: File '{transfer['file']}' received from {transfer['address']}")
            success = True
            if transfer["sync_file_handle"]:
                await asyncio.to_thread(transfer["sync_file_handle"].close)
                transfer["sync_file_handle"] = None # Avoid re-closing

            cli_wait_event = transfer.get("cli_wait_event") or self._transfer_completion_events.pop(transfer_id, None)
            if cli_wait_event: cli_wait_event.set()
            if transfer_id in self.transfers: del self.transfers[transfer_id]


    def get_transfer_status(self, transfer_id):
        # Ensure thread safety if transfers dict is modified elsewhere, though asyncio is single-threaded per loop
        transfer = self.transfers.get(transfer_id)
        if transfer:
            progress = 0
            if transfer["state"] == "sending":
                if transfer["total_bytes"] > 0:
                    progress = (transfer["bytes_sent_payload"] / transfer["total_bytes"]) * 100
            elif transfer["state"] == "receiving":
                progress = -1 # Unknown for receiving
            return {
                "id": transfer_id, "state": transfer["state"], "file": transfer["file"],
                "address": transfer["address"], "duration": time.time() - transfer["start_time"],
                "last_activity_ago": time.time() - transfer["last_activity"],
                "progress_pct": f"{progress:.2f}",
                "bytes_transferred": transfer.get("bytes_sent_payload") or transfer.get("bytes_received_payload",0),
                "total_bytes" : transfer.get("total_bytes", -1)
            }
        return None

    def cancel_transfer(self, transfer_id):
        if transfer_id in self.transfers:
            transfer = self.transfers.pop(transfer_id)
            logging.info(f"Transfer {transfer_id}: Canceled for file '{transfer['file']}'.")

            if transfer.get("sender") and hasattr(transfer["sender"], 'cancel'):
                asyncio.create_task(asyncio.to_thread(transfer["sender"].cancel))
            if transfer.get("receiver") and hasattr(transfer["receiver"], 'cancel'):
                asyncio.create_task(asyncio.to_thread(transfer["receiver"].cancel))

            if transfer.get("sync_file_handle"):
                asyncio.create_task(asyncio.to_thread(transfer["sync_file_handle"].close))
            
            cli_wait_event = transfer.get("cli_wait_event") or self._transfer_completion_events.pop(transfer_id, None)
            if cli_wait_event and not cli_wait_event.is_set():
                cli_wait_event.set() # Signal completion (as failure/cancellation)
            return True
        return False

    async def _check_timeouts_task(self):
        logging.info("Timeout checker started.")
        while self.running:
            now = time.time()
            for transfer_id in list(self.transfers.keys()): # Iterate on copy of keys
                transfer = self.transfers.get(transfer_id)
                if not transfer: continue
                if now - transfer["last_activity"] > TIMEOUT:
                    logging.warning(f"Transfer {transfer_id}: Timeout for '{transfer['file']}'. Canceling.")
                    self.cancel_transfer(transfer_id)
            await asyncio.sleep(5)
        logging.info("Timeout checker stopped.")

    async def run_forever(self):
        if not await self._connect_mesh():
            logging.error("Failed to connect to mesh. AkitaZmodemMeshCore cannot run.")
            self.running = False
            return

        self.running = True
        receive_task = asyncio.create_task(self._receive_loop_processor())
        timeout_task = asyncio.create_task(self._check_timeouts_task())
        logging.info("AkitaZmodemMeshCore is running in daemon mode. Waiting for transfers or Ctrl+C.")
        try:
            while self.running: await asyncio.sleep(1)
        except KeyboardInterrupt: logging.info("Daemon mode: Keyboard interrupt received.")
        finally:
            await self.stop() # Call full stop procedure
            # Wait for tasks to complete with a timeout - gather must await actual tasks
            try:
                await asyncio.wait_for(asyncio.gather(receive_task, timeout_task, return_exceptions=True), timeout=5.0)
            except asyncio.TimeoutError:
                logging.warning("Timeout waiting for background tasks to stop during shutdown.")
            logging.info("AkitaZmodemMeshCore daemon has shut down.")


    async def stop(self):
        if not self.running: return # Already stopping/stopped
        logging.info("Stopping AkitaZmodemMeshCore...")
        self.running = False
        for transfer_id in list(self.transfers.keys()): self.cancel_transfer(transfer_id)
        if self.mesh:
            try:
                await self.mesh.close()
                logging.info("Mesh connection closed.")
            except Exception as e: logging.error(f"Error closing mesh connection: {e}")
        # Other cleanup if necessary

    async def send_directory(self, mesh_core_address_str, directory_path, cli_wait_event=None):
        temp_zip_filename = f"temp_akita_dir_{int(time.time())}.zip"
        zip_buffer = io.BytesIO()
        success = False
        try:
            logging.info(f"Zipping directory '{directory_path}'...")
            def _zip_dir():
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for root, _, files in os.walk(directory_path):
                        for file_item in files:
                            file_path = os.path.join(root, file_item)
                            arcname = os.path.relpath(file_path, directory_path)
                            zipf.write(file_path, arcname)
            await asyncio.to_thread(_zip_dir)
            zip_buffer.seek(0)
            async with aiofiles.open(temp_zip_filename, "wb") as f_temp:
                await f_temp.write(zip_buffer.getvalue())
            logging.info(f"Directory zipped to '{temp_zip_filename}'. Starting send...")

            # For send_directory from CLI, create its own wait event if none passed
            # This is tricky as send_file itself is async.
            # The original cli_wait_event is for the entire send_directory op.
            dir_send_completion_event = asyncio.Event()
            transfer_id = await self.send_file(mesh_core_address_str, temp_zip_filename, dir_send_completion_event)

            if transfer_id is None:
                logging.error("Failed to initiate directory send (send_file failed).")
                if cli_wait_event: cli_wait_event.set(); return None # Signal outer failure
            
            await dir_send_completion_event.wait() # Wait for the send_file part to finish
            
            # Check if the transfer was successful by seeing if it's still in transfers (it shouldn't be if successful)
            if transfer_id not in self.transfers:
                 logging.info(f"Directory send for {temp_zip_filename} completed successfully.")
                 success = True
            else: # It's still there, meaning it probably failed or was cancelled
                 status = self.get_transfer_status(transfer_id)
                 logging.warning(f"Directory send for {temp_zip_filename} did not complete successfully. Final status: {status}")
                 success = False

            return transfer_id
        except Exception as e:
            logging.error(f"Error sending directory '{directory_path}': {e}", exc_info=True)
            return None
        finally:
            zip_buffer.close()
            if await asyncio.to_thread(os.path.exists, temp_zip_filename):
                await asyncio.to_thread(os.remove, temp_zip_filename)
            if cli_wait_event and not cli_wait_event.is_set(): cli_wait_event.set() # Ensure outer event is always set


    async def receive_directory(self, directory_path, overwrite=False, cli_wait_event=None):
        if not await asyncio.to_thread(os.path.exists, directory_path):
            await asyncio.to_thread(os.makedirs, directory_path)
        elif not await asyncio.to_thread(os.path.isdir, directory_path) :
             logging.error(f"Path {directory_path} exists and is not a directory.")
             if cli_wait_event: cli_wait_event.set(); return None

        temp_zip_filename = f"temp_akita_dir_recv_{int(time.time())}.zip"
        # For receive_directory from CLI, create its own wait event
        dir_recv_completion_event = asyncio.Event()
        transfer_id = await self.receive_file(temp_zip_filename, overwrite, dir_recv_completion_event)

        if not transfer_id:
            logging.error("Failed to initiate directory reception (receive_file failed).")
            if cli_wait_event: cli_wait_event.set(); return None
        
        logging.info(f"Transfer {transfer_id}: Waiting to receive directory archive '{temp_zip_filename}'.")
        try:
            await dir_recv_completion_event.wait() # Wait for receive_file to complete
        except asyncio.TimeoutError: # Should be handled by receive_file's timeout logic setting event
            logging.error(f"Transfer {transfer_id}: Timeout waiting for directory archive event.")
            self.cancel_transfer(transfer_id) # Ensure cleanup
            if cli_wait_event: cli_wait_event.set(); return None

        # Check if transfer was successful (file exists and transfer removed from list)
        if not await asyncio.to_thread(os.path.exists, temp_zip_filename) or transfer_id in self.transfers:
            logging.error(f"Transfer {transfer_id}: Zip file '{temp_zip_filename}' not found or receive failed. Extraction cancelled.")
            if cli_wait_event: cli_wait_event.set(); return None

        logging.info(f"Archive '{temp_zip_filename}' received. Extracting to '{directory_path}'.")
        try:
            def _unzip_dir():
                with zipfile.ZipFile(temp_zip_filename, 'r') as zipf: zipf.extractall(directory_path)
            await asyncio.to_thread(_unzip_dir)
            logging.info(f"Directory successfully extracted to '{directory_path}'.")
            return transfer_id
        except zipfile.BadZipFile: logging.error(f"BadZipFile: '{temp_zip_filename}'.")
        except Exception as e: logging.error(f"Error extracting '{temp_zip_filename}': {e}", exc_info=True)
        finally:
            if await asyncio.to_thread(os.path.exists, temp_zip_filename):
                await asyncio.to_thread(os.remove, temp_zip_filename)
            if cli_wait_event and not cli_wait_event.is_set(): cli_wait_event.set()


async def main():
    parser = argparse.ArgumentParser(description="Akita-Zmodem-MeshCore file transfer utility.")
    parser.add_argument("--config", default=CONFIG_FILE, help="Path to configuration file.")
    parser.add_argument("--mesh-type", choices=["serial", "tcp"], help="MeshCore conn type. Overrides config.")
    parser.add_argument("--serial-port", help="Serial port for MeshCore. Overrides config.")
    parser.add_argument("--serial-baud", type=int, help="Serial baud rate. Overrides config.")
    parser.add_argument("--tcp-host", help="TCP host for MeshCore. Overrides config.")
    parser.add_argument("--tcp-port", type=int, help="TCP port for MeshCore. Overrides config.")
    subparsers = parser.add_subparsers(dest="command", title="commands", required=False)

    send_parser = subparsers.add_parser("send", help="Send a file or directory.")
    send_parser.add_argument("address", type=str, help="Dest MeshCore node ID (e.g., !aabbccdd or a name).")
    send_parser.add_argument("path", help="File or directory path to send.")

    receive_parser = subparsers.add_parser("receive", help="Receive a file or directory.")
    receive_parser.add_argument("path", help="Destination path.")
    receive_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files.")

    status_parser = subparsers.add_parser("status", help="Get transfer status.")
    status_parser.add_argument("transfer_id", type=int, help="Transfer ID.")

    cancel_parser = subparsers.add_parser("cancel", help="Cancel a transfer.")
    cancel_parser.add_argument("transfer_id", type=int, help="Transfer ID.")
    args = parser.parse_args()

    # Build config overrides from CLI args
    cli_cfg_overrides = {}
    if args.mesh_type: cli_cfg_overrides["mesh_connection_type"] = args.mesh_type
    if args.serial_port: cli_cfg_overrides["mesh_serial_port"] = args.serial_port
    if args.serial_baud: cli_cfg_overrides["mesh_serial_baud"] = args.serial_baud
    if args.tcp_host: cli_cfg_overrides["mesh_tcp_host"] = args.tcp_host
    if args.tcp_port: cli_cfg_overrides["tcp_port"] = args.tcp_port # Corrected key

    zmodem_app = AkitaZmodemMeshCore(cli_config_overrides=cli_cfg_overrides)
    cli_completion_event = None

    try:
        if args.command:
            if not await zmodem_app._connect_mesh():
                logging.error("Cannot execute command, mesh connection failed.")
                return
            
            cli_completion_event = asyncio.Event()

            if args.command == "send":
                if await asyncio.to_thread(os.path.isdir, args.path):
                    await zmodem_app.send_directory(args.address, args.path, cli_completion_event)
                else:
                    await zmodem_app.send_file(args.address, args.path, cli_completion_event)
                logging.info(f"Send command for '{args.path}' initiated. Waiting for completion...")
                await cli_completion_event.wait()
                logging.info(f"Send command for '{args.path}' finished processing.")

            elif args.command == "receive":
                is_dir_reception = not (args.path.endswith(".zip") or "." in os.path.basename(args.path) and os.path.isfile(args.path)) # Heuristic
                if is_dir_reception :
                    await zmodem_app.receive_directory(args.path, args.overwrite, cli_completion_event)
                else:
                    await zmodem_app.receive_file(args.path, args.overwrite, cli_completion_event)
                logging.info(f"Receive command for '{args.path}' initiated. Waiting for completion...")
                await cli_completion_event.wait()
                logging.info(f"Receive command for '{args.path}' finished processing.")

            elif args.command == "status":
                status = zmodem_app.get_transfer_status(args.transfer_id)
                print(json.dumps(status, indent=4) if status else f"Transfer ID {args.transfer_id} not found.")
            elif args.command == "cancel":
                print(f"Transfer {args.transfer_id} cancellation {'successful' if zmodem_app.cancel_transfer(args.transfer_id) else 'failed or not found'}.")
            
            # For commands that don't need long background processing after the command itself returns.
            if args.command in ["status", "cancel"] and cli_completion_event:
                cli_completion_event.set() # Ensure it's set if not a transfer command

        else: # No command, run as a daemon
            await zmodem_app.run_forever()

    except KeyboardInterrupt:
        logging.info("Main: Keyboard interrupt received.")
    finally:
        logging.info("Main: Shutting down...")
        await zmodem_app.stop() # Ensure app is stopped and resources cleaned up
        logging.info("Main: Application has shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("AkitaZmodemMeshCore (top-level) stopped by user.")
    except Exception as e:
        logging.error(f"Unhandled top-level error: {e}", exc_info=True)
