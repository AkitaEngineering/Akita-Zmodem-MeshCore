# Akita-Zmodem-MeshCore

Akita-Zmodem-MeshCore is a robust file transfer utility for MeshCore networks, enabling reliable file transfers over low-bandwidth, high-latency mesh networks using the zModem protocol.

## Features

* **zModem Protocol:** Implements the zModem protocol for reliable file transfers.
* **MeshCore Integration:** Utilizes MeshCore's packet routing for efficient data transmission.
* **Asynchronous Operations:** Uses `asyncio` for non-blocking I/O, improving performance.
* **Directory Transfer:** Supports sending and receiving entire directories using zip archives.
* **Timeout Handling:** Includes timeout mechanisms to handle network interruptions.
* **Configuration File:** Allows customization of settings through a JSON configuration file.
* **Command-Line Interface (CLI):** Provides a user-friendly CLI for managing transfers.
* **Status Reporting:** Provides detailed transfer status information.
* **Transfer Cancellation:** Allows users to cancel active transfers.
* **File Overwrite Protection:** Prevents accidental file overwrites.
* **Logging:** Comprehensive logging for debugging and error reporting.

## Installation

1.  Ensure you have Python 3.7+ installed.
2.  Install the required libraries:

    ```bash
    pip install meshcore zmodem
    ```

## Usage

1.  **Configuration:**
    * Run the script once to generate the `akita_zmodem_meshcore_config.json` file.
    * Edit the configuration file to customize settings such as `zmodem_port`, `chunk_size`, and `timeout`.

2.  **Running the Utility:**
    * Open a terminal and navigate to the directory where you saved `akita_zmodem_meshcore.py`.
    * Use the following commands:

    * **Send a file or directory:**

        ```bash
        python akita_zmodem_meshcore.py send <address> <path>
        ```

        * `<address>`: Destination MeshCore address.
        * `<path>`: File or directory path to send.

    * **Receive a file or directory:**

        ```bash
        python akita_zmodem_meshcore.py receive <path> [--overwrite]
        ```

        * `<path>`: Destination path to receive the file or directory.
        * `--overwrite`: (Optional) Overwrite existing files.

    * **Get transfer status:**

        ```bash
        python akita_zmodem_meshcore.py status <transfer_id>
        ```

        * `<transfer_id>`: Transfer ID.

    * **Cancel a transfer:**

        ```bash
        python akita_zmodem_meshcore.py cancel <transfer_id>
        ```

        * `<transfer_id>`: Transfer ID.

    * **Run the receiver loop:**

        ```bash
        python akita_zmodem_meshcore.py
        ```

        This command will run the receiver loop, and wait for incoming file transfers.

## Example

```bash
# Send a file
python akita_zmodem_meshcore.py send 12345 my_file.txt

# Receive a file
python akita_zmodem_meshcore.py receive received_file.txt

# Get transfer status
python akita_zmodem_meshcore.py status 1

# Cancel a transfer
python akita_zmodem_meshcore.py cancel 1
```

## Configuration File (akita_zmodem_meshcore_config.json)

```json
{
    "zmodem_port": 2000,
    "chunk_size": 256,
    "timeout": 30
}
```
* `zmodem_port`: The port number used for zModem communication.
* `chunk_size`: The size of data chunks sent over the network.
* `timeout`: The timeout duration (in seconds) for transfer operations.

## Contributing

Contributions are welcome! Please feel free to submit pull requests or open issues for bug reports and feature requests.
