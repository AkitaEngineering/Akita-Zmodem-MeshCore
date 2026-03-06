# Akita-Zmodem-MeshCore

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**Akita-Zmodem-MeshCore** is a robust file transfer utility developed by Akita Engineering ([www.akitaengineering.com](http://www.akitaengineering.com)). It enables reliable file and directory transfers over low-bandwidth, high-latency MeshCore networks (based on [ripplebiz/MeshCore](https://github.com/ripplebiz/MeshCore)) using the venerable Zmodem protocol.

This utility is designed for asynchronous operation, making it suitable for environments where network responsiveness can vary.

## Key Features

* **Built‑in Zmodem Protocol**: A fully self‑contained, Z‑modem‑like implementation lives in `zmodem.py`, so there is no requirement to install an external Zmodem library. The code handles handshakes, CRC32 framing, resumable transfers, and per‑chunk port headers for reliable operation over the mesh.
* **MeshCore Integration**: Uses Python bindings for MeshCore networks (tested with `fdlamotte/meshcore_py` but compatible with any API providing `MeshCore.create_*`, `mesh.subscribe`, and `mesh.commands.send_msg`).
* **Async I/O with asyncio**: Non‑blocking operations keep transfers responsive even on poor links.
* **File & Directory Support**: Send files directly or zip directories on‑the‑fly; received zips are automatically extracted.
* **Chunk Header Handling**: Data sent over the mesh is split into `mesh_packet_chunk_size` fragments; each fragment begins with an application port header so the receiver can reassemble correctly.
* **Configurable Timeouts**: Automatic cancellation of stalled transfers.
* **JSON Configuration**: Tweak ports, chunk sizes, MeshCore connection params, and more via `akita_zmodem_meshcore_config.json` or CLI overrides.
* **Robust CLI**: Commands (`send`, `receive`, `status`, `cancel`) either run as a one‑off or the script can operate as a long‑running daemon.
* **Daemon Mode & Status**: Run as a listener and query active transfers.
* **Cancellation & Safety**: Cancel mid‑transfer and protect against unwanted overwrites.
* **Detailed Logging**: Built‑in logging at INFO/DEBUG levels aids debugging and monitoring.

## Important Note on Dependencies

Because the Zmodem protocol is implemented internally, the only mandatory third‑party dependency is the `meshcore` Python library (to communicate with a MeshCore radio).  You do **not** need to install any separate `zmodem` package; if one is present it will be ignored in favor of the built‑in implementation.

* **MeshCore Python Library** – must expose the basic API used here (`MeshCore.create_serial`/`create_tcp`, `mesh.subscribe`, `mesh.commands.send_msg`, etc.).  This library handles the low‑level radio/TCP transport.
* **Optional**: `tqdm` for progress bars in CLI sessions (not required).  If absent, the program falls back to plain logging.

All other functionality is self‑contained within the repository.

## Installation

1.  **Python 3.8+ is recommended.**
2.  **Clone the repository (or download the script).**
3.  **Create and activate a virtual environment (recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
4.  **Install the required Python libraries:**
    ```bash
    pip install -r requirements.txt
    ```
    This will attempt to install `meshcore`, `zmodem`, `tqdm`, and `crcmod` (often a zmodem dependency). Ensure `meshcore` installs a library compatible with the script's usage.

## Configuration

1.  **Generate Configuration File**: The configuration file `akita_zmodem_meshcore_config.json` will be created with default settings in the same directory as the script if it doesn't exist on the first run.
    ```bash
    python akita_zmodem_meshcore.py --help # Running with --help or any command will generate it
    ```

2.  **Edit Configuration**: Modify `akita_zmodem_meshcore_config.json` to suit your needs. Key settings include:
    * `zmodem_app_port`: An application-level port number used to distinguish Zmodem traffic over MeshCore.
    * `mesh_packet_chunk_size`: Maximum size of data chunks sent over the mesh network (after Zmodem protocol data and app port are added).
    * `timeout`: Transfer timeout in seconds.
    * `mesh_connection_type`: How to connect to your MeshCore device (`serial` or `tcp`).
    * `mesh_serial_port`, `mesh_serial_baud`: Settings for serial connection.
    * `mesh_tcp_host`, `mesh_tcp_port`: Settings for TCP connection.

    These MeshCore connection settings can also be overridden via CLI arguments. See `docs/CONFIGURATION.md` for more details.

## Usage

The utility is controlled via command-line arguments.

```bash
python akita_zmodem_meshcore.py [OPTIONS] [COMMAND] [COMMAND_ARGS]
```

Global Options (Optional, override config file):

```
--config <path>          Path to JSON configuration file (created if missing)
--mesh-type [serial|tcp] Override connection type
--serial-port <PORT_PATH> (e.g., /dev/ttyUSB0, COM3)
--serial-baud <BAUDRATE>
--tcp-host <HOST_IP_OR_NAME>
--tcp-port <PORT_NUMBER>

```
--mesh-type [serial|tcp]
--serial-port <PORT_PATH> (e.g., /dev/ttyUSB0, COM3)
--serial-baud <BAUDRATE>
--tcp-host <HOST_IP_OR_NAME>
--tcp-port <PORT_NUMBER>
```

Commands:

Run as a daemon (listener mode):

```bash
python akita_zmodem_meshcore.py
# (add connection args if not in config or to override)
```

Send a file or directory:

```bash
python akita_zmodem_meshcore.py send <destination_node_id> <path/to/file_or_dir>
```

`<destination_node_id>`: String identifier for the target MeshCore node (e.g., !aabbccdd or a name your MeshCore setup recognizes).

Receive a file or directory:

```bash
python akita_zmodem_meshcore.py receive <path/to/save_location> [--overwrite] [--directory]
```

* `--directory` forces the path to be treated as a directory even if it does not
  yet exist or lacks a recognisable extension.  The utility will also infer
  directory mode automatically when the target already exists and is a
  directory.

Note: For directory reception, provide the path where the directory's contents should be extracted.

Get transfer status:

```bash
python akita_zmodem_meshcore.py status <transfer_id>
```

Cancel a transfer:

```bash
python akita_zmodem_meshcore.py cancel <transfer_id>
```

See `docs/USAGE.md` for detailed command explanations and examples.

### Example

```bash
# On Machine A (Sender):
# Ensure your config file is set up or use CLI overrides for connection:
# e.g., python akita_zmodem_meshcore.py --mesh-type serial --serial-port /dev/ttyUSB0

# Send a file to node with ID "!TargetNodeHexID"
python akita_zmodem_meshcore.py send "!TargetNodeHexID" my_document.txt

# On Machine B (Receiver - running in daemon mode):
# Start the listener (it will use its config for connection)
python akita_zmodem_meshcore.py

# Alternatively, to pre-designate where a file should go on Machine B:
# python akita_zmodem_meshcore.py receive received_files/my_document.txt [--overwrite]
# This command will wait for the specific transfer to complete.
```

## Contributing

Contributions are welcome! Please feel free to submit pull requests or open issues for bug reports and feature requests.  
See `CONTRIBUTING.md` for more details.

## License

This project is licensed under the GNU General Public License v3.0. See the LICENSE file for details.
