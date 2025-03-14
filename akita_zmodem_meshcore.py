import meshcore
import zmodem
import time
import asyncio
import logging
import os
import zipfile
import io
import argparse
import json

# Configuration
CONFIG_FILE = "akita_zmodem_meshcore_config.json"
DEFAULT_CONFIG = {
    "zmodem_port": 2000,
    "chunk_size": 256,
    "timeout": 30
}

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG
    except json.JSONDecodeError:
        logging.error("Invalid configuration file. Using default settings.")
        return DEFAULT_CONFIG

config = load_config()
ZMODEM_PORT = config["zmodem_port"]
CHUNK_SIZE = config["chunk_size"]
TIMEOUT = config["timeout"]

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class AkitaZmodemMeshCore:
    def __init__(self, mesh):
        self.mesh = mesh
        self.transfers = {}
        self.transfer_id_counter = 0
        self.running = True

    def generate_transfer_id(self):
        self.transfer_id_counter += 1
        return self.transfer_id_counter

    async def send_file(self, mesh_core_address, filepath):
        transfer_id = self.generate_transfer_id()
        self.transfers[transfer_id] = {
            "state": "sending",
            "sender": zmodem.Sender(open(filepath, "rb")),
            "file": filepath,
            "address": mesh_core_address,
            "start_time": time.time(),
            "last_activity": time.time(),
            "bytes_sent": 0,
            "total_bytes": os.path.getsize(filepath),
        }
        logging.info(f"Transfer {transfer_id}: Sending file '{filepath}' to {mesh_core_address}")
        await self.send_loop(transfer_id)

    async def send_loop(self, transfer_id):
        transfer = self.transfers[transfer_id]
        sender = transfer["sender"]
        address = transfer["address"]

        while self.running and not sender.is_finished():
            try:
                data = sender.get_next_packet()
                if data:
                    chunks = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
                    for chunk in chunks:
                        packet = meshcore.Packet(
                            destination=address, port=ZMODEM_PORT, payload=chunk
                        )
                        self.mesh.send_packet(packet)
                        transfer["bytes_sent"] += len(chunk)
                        transfer["last_activity"] = time.time()
                        await asyncio.sleep(0.01)
                else:
                    await asyncio.sleep(0.1)
            except Exception as e:
                logging.error(f"Transfer {transfer_id}: Send error: {e}")
                self.cancel_transfer(transfer_id)
                return

        logging.info(f"Transfer {transfer_id}: File '{transfer['file']}' sent to {address}")
        del self.transfers[transfer_id]

    async def receive_file(self, filepath, overwrite=False):
        if os.path.exists(filepath) and not overwrite:
            logging.error(f"File '{filepath}' already exists. Use overwrite=True to overwrite.")
            return None
        transfer_id = self.generate_transfer_id()
        self.transfers[transfer_id] = {
            "state": "receiving",
            "receiver": None,
            "file": filepath,
            "address": None,
            "start_time": time.time(),
            "last_activity": time.time(),
            "received_data": b"",
            "bytes_received": 0,
        }
        logging.info(f"Transfer {transfer_id}: Receiving file to '{filepath}'")
        return transfer_id

    async def receive_loop(self):
        while self.running:
            packet = self.mesh.receive_packet()
            if packet and packet.port == ZMODEM_PORT:
                await self.process_received_packet(packet)
            await asyncio.sleep(0.01)

    async def process_received_packet(self, packet):
        for transfer_id, transfer in self.transfers.items():
            if transfer["state"] == "receiving":
                if transfer["address"] is None:
                    transfer["address"] = packet.source
                    transfer["receiver"] = zmodem.Receiver(open(transfer["file"], "wb"))
                if packet.source == transfer["address"]:
                    transfer["received_data"] += packet.payload
                    transfer["bytes_received"] += len(packet.payload)
                    transfer["last_activity"] = time.time()
                    while transfer["received_data"]:
                        try:
                            result = transfer["receiver"].receive(transfer["received_data"])
                            if result is None:
                                break
                            transfer["received_data"] = b""
                            if transfer["receiver"].is_finished():
                                logging.info(f"Transfer {transfer_id}: File '{transfer['file']}' received from {transfer['address']}")
                                del self.transfers[transfer_id]
                                return
                        except Exception as e:
                            logging.error(f"Transfer {transfer_id}: Receive error: {e}")
                            self.cancel_transfer(transfer_id)
                            return

    def get_transfer_status(self, transfer_id):
        if transfer_id in self.transfers:
            transfer = self.transfers[transfer_id]
            if transfer["state"] == "sending":
                progress = transfer["bytes_sent"] / transfer["total_bytes"] if transfer["total_bytes"] > 0 else 0
            else:
                progress = 0
            return {
                "state": transfer["state"],
                "file": transfer["file"],
                "address": transfer["address"],
                "duration": time.time() - transfer["start_time"],
                "progress": progress
            }
        else:
            return None

    def cancel_transfer(self, transfer_id):
        if transfer_id in self.transfers:
            logging.info(f"Transfer {transfer_id}: Canceled.")
            del self.transfers[transfer_id]

    async def run(self):
        while self.running:
            await self.receive_loop()
            await asyncio.sleep(1)
            self.check_timeouts()

    def check_timeouts(self):
        now = time.time()
        for transfer_id, transfer in list(self.transfers.items()):
            if now - transfer["last_activity"] > TIMEOUT:
                logging.warning(f"Transfer {transfer_id}: Timeout exceeded.")
                self.cancel_transfer(transfer_id)

    def stop(self):
        self.running = False

    async def send_directory(self, mesh_core_address, directory_path):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(directory_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, directory_path)
                    zipf.write(file_path, arcname)
        zip_buffer.seek(0)
        temp_file = "temp_dir.zip"
        with open(temp_file, "wb") as f:
            f.write(zip_buffer.read())

        await self.send_file(mesh_core_address, temp_file)
        os.remove(temp_file)

    async def receive_directory(self, directory_path, overwrite=False):
        temp_file = "temp_dir.zip"
        transfer_id = await self.receive_file(temp_file, overwrite)
        if transfer_id:
            while transfer_id in self.transfers:
                await asyncio.sleep(0.1)
            try:
                with zipfile.ZipFile(temp_file, 'r') as zipf:
                    zipf.extractall(directory_path)
                  os.remove(temp_file)
                logging.info(f"Directory received and extracted to {directory_path}")
            except Exception as e:
                logging.error(f"Error extracting directory: {e}")
        return transfer_id

async def main():
    parser = argparse.ArgumentParser(description="Akita-Zmodem-MeshCore file transfer utility.")
    subparsers = parser.add_subparsers(dest="command")

    send_parser = subparsers.add_parser("send", help="Send a file or directory.")
    send_parser.add_argument("address", type=int, help="Destination MeshCore address.")
    send_parser.add_argument("path", help="File or directory path.")

    receive_parser = subparsers.add_parser("receive", help="Receive a file or directory.")
    receive_parser.add_argument("path", help="Destination path.")
    receive_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files.")

    status_parser = subparsers.add_parser("status", help="Get transfer status.")
    status_parser.add_argument("transfer_id", type=int, help="Transfer ID.")

    cancel_parser = subparsers.add_parser("cancel", help="Cancel a transfer.")
    cancel_parser.add_argument("transfer_id", type=int, help="Transfer ID.")

    args = parser.parse_args()

    mesh = meshcore.Mesh()
    zmodem_app = AkitaZmodemMeshCore(mesh)

    if args.command == "send":
        if os.path.isdir(args.path):
            await zmodem_app.send_directory(args.address, args.path)
        else:
            await zmodem_app.send_file(args.address, args.path)
    elif args.command == "receive":
        if args.path.endswith(".zip"):
            await zmodem_app.receive_directory(args.path[:-4], args.overwrite)
        else:
            await zmodem_app.receive_file(args.path, args.overwrite)
    elif args.command == "status":
        status = zmodem_app.get_transfer_status(args.transfer_id)
        if status:
            print(json.dumps(status, indent=4))
        else:
            print("Transfer not found.")
    elif args.command == "cancel":
        zmodem_app.cancel_transfer(args.transfer_id)
        print("Transfer canceled.")
    else:
        await zmodem_app.run()

if __name__ == "__main__":
    asyncio.run(main())
