# Akita-Zmodem-MeshCore

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**Akita-Zmodem-MeshCore** is a robust file transfer utility developed by Akita Engineering ([www.akitaengineering.com](http://www.akitaengineering.com)). It enables reliable file and directory transfers over low-bandwidth, high-latency MeshCore networks (based on [ripplebiz/MeshCore](https://github.com/ripplebiz/MeshCore)) using the venerable Zmodem protocol.

This utility is designed for asynchronous operation, making it suitable for environments where network responsiveness can vary.

## Key Features

* **Zmodem Protocol**: Implements Zmodem for reliable file transfers. (Full resumability depends on session state and Zmodem library capabilities).
* **MeshCore Integration**: Adapted to utilize Python bindings for MeshCore networks (specifically targeting `fdlamotte/meshcore_py` or a compatible API).
* **Asynchronous Operations**: Uses `asyncio` for non-blocking I/O, improving performance and responsiveness.
* **File and Directory Transfer**: Supports sending and receiving individual files and entire directories (via zip archives).
* **Timeout Handling**: Includes mechanisms to handle network interruptions and transfer timeouts.
* **JSON Configuration**: Allows customization of settings (application port, mesh packet chunk sizes, timeouts, MeshCore connection details) via an `akita_zmodem_meshcore_config.json` file.
* **Command-Line Interface (CLI)**: Provides a user-friendly CLI for initiating transfers, checking status, and canceling operations. CLI commands for send/receive now wait for transfer completion.
* **Daemon Mode**: Can run as a background listener for incoming transfers.
* **Status Reporting**: Offers detailed transfer status information.
* **Transfer Cancellation**: Allows users to cancel active transfers.
* **File Overwrite Protection**: Prevents accidental file overwrites unless specified.
* **Logging**: Comprehensive logging for debugging and operational monitoring.

## Important Note on Dependencies

This version of Akita-Zmodem-MeshCore has been significantly refactored to work with modern asynchronous Python practices and specific libraries:

* **MeshCore Python Library**: It is designed to work with a `meshcore` Python library compatible with the API used (e.g., `fdlamotte/meshcore_py`). You'll need a MeshCore device (like a LoRa radio flashed with MeshCore firmware) that this library can connect to (e.g., via Serial or TCP). The script expects methods like `MeshCore.create_serial()`, `mesh.subscribe()`, and `mesh.commands.send_msg()`.
* **Zmodem Python Library**: It assumes a standard Python `zmodem` library is installed (`pip install zmodem`). The behavior of this utility is dependent on the specific API and blocking nature of the chosen Zmodem library. Blocking calls are wrapped to run in separate threads. The mock objects in the script provide a basic API contract if the library is missing.

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
python akita_zmodem_meshcore.py [CONNECTION_ARGS] [COMMAND] [COMMAND_ARGS]
```

Global Connection Arguments (Optional, override config):

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
python akita_zmodem_meshcore.py receive <path/to/save_location> [--overwrite]
```

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
