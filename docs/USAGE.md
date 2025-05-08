# Akita-Zmodem-MeshCore Usage Guide

This guide provides detailed information on how to use the Akita-Zmodem-MeshCore utility.

## General Syntax

```bash
python akita_zmodem_meshcore.py [CONNECTION_ARGS] [COMMAND] [COMMAND_ARGS]
```

If no COMMAND is provided, the utility runs in daemon/listener mode, waiting for incoming transfers. When a command like `send` or `receive` is used from the CLI, the script will now wait for that specific operation to complete before exiting.

---

## Connection Arguments

These arguments override settings in the `akita_zmodem_meshcore_config.json` file, allowing you to specify how to connect to your MeshCore device.

- `--mesh-type [serial|tcp]`: Specify the connection type.  
  Example: `--mesh-type serial`

- `--serial-port <PORT_PATH>`: The serial port device name (e.g., `/dev/ttyUSB0` on Linux, `COM3` on Windows).  
  Example: `--serial-port /dev/ttyUSB0`

- `--serial-baud <BAUDRATE>`: The baud rate for the serial connection.  
  Example: `--serial-baud 115200`

- `--tcp-host <HOST_IP_OR_NAME>`: The hostname or IP address of the MeshCore device if using TCP.  
  Example: `--tcp-host 192.168.1.10`

- `--tcp-port <PORT_NUMBER>`: The TCP port number on the MeshCore device.  
  Example: `--tcp-port 4403`

---

## Commands

### 1. Daemon Mode (Listener)

If you run the script without any specific command, it starts in daemon mode. It will connect to the MeshCore network (using configuration file settings or CLI overrides) and listen for incoming Zmodem transfers.

Received files will be saved based on pre-declared receive commands or, if not pre-declared, the Zmodem protocol's filename. (This part of auto-reception to an arbitrary path based on sender's filename would be an advanced feature not fully implemented; currently, a receive "slot" is generally needed.)

**Examples:**

```bash
# Using settings from config file
python akita_zmodem_meshcore.py

# Overriding serial port from config
python akita_zmodem_meshcore.py --serial-port /dev/ttyS0
```

Press `Ctrl+C` to stop the daemon.

---

### 2. `send <destination_node_id> <path>`

Sends a file or an entire directory to a specified destination node on the MeshCore network.

- `<destination_node_id>`: The unique identifier (string) of the target MeshCore node (e.g., `!aabbccdd` or a resolved name).
- `<path>`: The local path to the file or directory you want to send.

**Examples:**

```bash
# Send a single file
python akita_zmodem_meshcore.py send "!TargetNodeHexID" /path/to/my_document.txt

# Send an entire directory
python akita_zmodem_meshcore.py send "NodeB_Name" /path/to/my_project_folder
```

**Behavior:** The `send` command initiates the transfer and the script will remain active, showing logs, until this specific transfer completes, is canceled, or times out.

---

### 3. `receive <path> [--overwrite]`

Prepares the utility to receive an incoming file or directory and save it to the specified local path.

- `<path>`:
  - For a single file: Full path (including filename) where the file will be saved.
  - For a directory: Path to the local directory where the received zipped contents will be extracted.

- `--overwrite` (optional): Overwrites existing files or directories if present.

**Examples:**

```bash
# Receive a single file
python akita_zmodem_meshcore.py receive /home/user/downloads/incoming_report.pdf

# Receive a directory
python akita_zmodem_meshcore.py receive /home/user/received_projects/ --overwrite
```

**Behavior:** This sets up a "slot" for an incoming transfer and waits. Reception occurs when a remote node sends a file that matches this receive slot.

---

### 4. `status <transfer_id>`

Displays the current status of an active or recently completed/failed transfer.

- `<transfer_id>`: The numerical ID of the transfer.

**Example:**

```bash
python akita_zmodem_meshcore.py status 1
```

Output will be a JSON object with details like state, file, progress percentage, etc.

---

### 5. `cancel <transfer_id>`

Attempts to cancel an ongoing transfer.

- `<transfer_id>`: The numerical ID of the transfer to cancel.

**Example:**

```bash
python akita_zmodem_meshcore.py cancel 1
```

The utility will attempt to stop the transfer and clean up associated resources. The success message will indicate if the cancellation was processed.

---

## Notes on Operation

- **Node IDs**: The `<destination_node_id>` used in the `send` command must be a valid identifier recognized by your MeshCore setup (e.g. `!aabbccdd`, public key, or name).
  
- **Transfer IDs**: These are logged when a transfer starts and are required for `status` and `cancel` commands.

- **Single CLI Operation at a Time**: CLI usage focuses on one transfer at a time. For general listening or background operation, run the utility in daemon mode (i.e., without a command).

