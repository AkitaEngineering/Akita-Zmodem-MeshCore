# Akita-Zmodem-MeshCore Configuration

The Akita-Zmodem-MeshCore utility uses a JSON configuration file to manage its settings. By default the file is named `akita_zmodem_meshcore_config.json` and will be created with default values in the same directory as the script if it doesn't already exist.

If you prefer to keep a configuration in a different location, the `--config` command‑line option lets you specify an alternate file path; the file will similarly be created if it is missing.

You can also override several MeshCore connection parameters using command-line arguments.

## Configuration File (`akita_zmodem_meshcore_config.json`)

Here's an explanation of each field in the configuration file:

* `"zmodem_app_port": 2001`  
    * **Description**: An application-level port number. Since the underlying MeshCore messaging (as handled by `meshcore_py`) might not have a built-in port concept for multiplexing different applications on the same node, this utility prepends this port number to its data packets. The receiving end uses this to identify Zmodem traffic intended for this application.  
    * **Type**: Integer  
    * **Default**: `2001`

* `"chunk_size": 256`  
    * **Description**: The size of blocks read from the file by the internal Zmodem sender implementation.  (In previous versions this parameter was only passed through to an external zmodem library, so its effect depended on that library.)  The mesh network still chunks the data further according to `mesh_packet_chunk_size`.  
    * **Type**: Integer  
    * **Default**: `256`

* `"mesh_packet_chunk_size": 200`  
    * **Description**: The maximum size (in bytes) of the payload for a single packet sent over the MeshCore network. This is after the `zmodem_app_port` has been prepended. Zmodem protocol packets can be larger than what the underlying mesh radio can handle in one go. This setting defines how the utility should break down larger Zmodem protocol data units into smaller chunks suitable for the mesh.  
    * **Type**: Integer  
    * **Default**: `200` (Adjust based on your MeshCore device's effective MTU and network conditions to avoid fragmentation by lower layers or packet loss).

* `"timeout": 60`  
    * **Description**: The duration (in seconds) of inactivity after which an ongoing transfer is considered timed-out and subsequently canceled. Activity is defined as successfully sending or receiving data chunks relevant to the Zmodem transfer.  
    * **Type**: Integer  
    * **Default**: `60`

* `"mesh_connection_type": "serial"`  
    * **Description**: Specifies the method used to connect to the local MeshCore device/interface that the `meshcore_py` library will use.  
    * **Values**: `"serial"` or `"tcp"`  
    * **Default**: `"serial"`

* `"mesh_serial_port": "/dev/ttyUSB0"`  
    * **Description**: If `mesh_connection_type` is `"serial"`, this is the serial port device name (e.g., `/dev/ttyUSB0` on Linux, `COM3` on Windows) where the MeshCore radio is connected.  
    * **Type**: String  
    * **Default**: `"/dev/ttyUSB0"`

* `"mesh_serial_baud": 115200`  
    * **Description**: If `mesh_connection_type` is `"serial"`, this is the baud rate for the serial communication with the MeshCore device.  
    * **Type**: Integer  
    * **Default**: `115200`

* `"mesh_tcp_host": "127.0.0.1"`  
    * **Description**: If `mesh_connection_type` is `"tcp"`, this is the hostname or IP address of the MeshCore device/interface that accepts TCP connections. This could be a local IP if the MeshCore device is connected to the host machine and exposed via a TCP serial bridge, or if `meshcore_py` connects to a TCP endpoint of a MeshCore daemon/gateway.  
    * **Type**: String  
    * **Default**: `"127.0.0.1"`

* `"mesh_tcp_port": 4403`  
    * **Description**: If `mesh_connection_type` is `"tcp"`, this is the TCP port number for the MeshCore connection. (Note: 4403 is commonly associated with Meshtastic; ensure this is the correct port for your MeshCore TCP service).  
    * **Type**: Integer  
    * **Default**: `4403`

## Command-Line Overrides for Connection Parameters

You can override the MeshCore connection settings from the configuration file using the following command-line arguments when running `akita_zmodem_meshcore.py`:

* `--mesh-type [serial|tcp]`: Overrides `mesh_connection_type`.
* `--serial-port <PORT_PATH>`: Overrides `mesh_serial_port`.
* `--serial-baud <BAUDRATE>`: Overrides `mesh_serial_baud`.
* `--tcp-host <HOST_IP_OR_NAME>`: Overrides `mesh_tcp_host`.
* `--tcp-port <PORT_NUMBER>`: Overrides `mesh_tcp_port`.

**Example:**

If your config file specifies serial connection, but you want to use TCP for one run:

```bash
python akita_zmodem_meshcore.py --mesh-type tcp --tcp-host 192.168.1.50 --tcp-port 6500 send "!NodeAlpha" /myfiles/data.bin
```

## Examples: Real MeshCore Setups

Below are example configuration snippets and operational notes for common
MeshCore setups. These are practical starting points — adapt values to your
local network and device.

1) Serial-connected MeshCore (USB radio)

```json
{
    "mesh_connection_type": "serial",
    "mesh_serial_port": "/dev/ttyUSB0",
    "mesh_serial_baud": 115200,
    "zmodem_app_port": 2001,
    "mesh_packet_chunk_size": 200
}
```

Notes:
- Ensure the user running the script has read/write access to the serial
    device (e.g., member of the `dialout` group on many Linux systems).
- Confirm the device node with `ls -l /dev/ttyUSB0` (or the appropriate
    device path). If using a USB-to-serial adapter, the device name may differ
    (e.g., `/dev/ttyACM0`).

2) TCP bridge to a MeshCore daemon (remote or local)

If your MeshCore device is exposed over TCP (for example via a bridge or a
daemon on a gateway machine), use the TCP connection type:

```json
{
    "mesh_connection_type": "tcp",
    "mesh_tcp_host": "192.168.1.100",
    "mesh_tcp_port": 4403,
    "zmodem_app_port": 2001,
    "mesh_packet_chunk_size": 200
}
```

Notes:
- Make sure the host/port are reachable from the machine running
    `akita_zmodem_meshcore.py` (test with `nc`/`telnet` or `ss`/`netcat`).
- The MeshCore Python client (the library that exposes `MeshCore.create_tcp`)
    must speak the same protocol as the remote service.

3) Example `systemd` unit (daemon mode)

Create `/etc/systemd/system/akita-zmodem.service`:

```ini
[Unit]
Description=Akita Zmodem MeshCore daemon
After=network.target

[Service]
User=akita
Group=akita
WorkingDirectory=/opt/akita/akita-zmodem
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/akita/venv/bin/python /opt/akita/akita-zmodem-meshcore/akita_zmodem_meshcore.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Adjust `User`, `WorkingDirectory`, and `ExecStart` to match your install. Use
`sudo systemctl daemon-reload && sudo systemctl enable --now akita-zmodem` to
start and enable the service.

4) Troubleshooting

- Serial permission denied: ensure the running user belongs to the group that
    owns the serial device (e.g., `sudo usermod -aG dialout $USER`).
- Wrong device: check dmesg output after plugging the radio to find the
    correct `/dev/tty*` node.
- TCP connection refused: verify the bridge/daemon is listening on the given
    host/port and there are no firewall rules blocking access.
- Fragmentation or errors: reduce `mesh_packet_chunk_size` to accommodate a
    smaller MTU on your radio link; conservative defaults are 200 bytes.


This command will attempt to connect via TCP to 192.168.1.50 on port 6500, overriding any serial settings in `akita_zmodem_meshcore_config.json` for this specific execution.
