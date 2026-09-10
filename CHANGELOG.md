# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

- Recover from failed sends and lost or duplicated data fragments; prevent
  stale ACKs, repeated STARTs, and premature ENDs from advancing or resetting
  transfers. Running receivers can repeat a lost final confirmation.
- Assemble and validate complete START headers before selecting filenames or
  accepting file sizes. Enforce advertised sizes before writing data.
- Serialize outgoing frame fragments, route active peers before waiting receive
  slots, report payload progress, and stop send tasks when canceled. Outbound
  retries no longer keep an unresponsive peer alive indefinitely.
- Reject ZIP paths escaping through existing symlinks, preserve empty
  directories, and enforce uncompressed size limits when sending directories.
  Canceled directory transfers are not extracted.
- Reject null, boolean, and non-finite numeric configuration values; return
  nonzero CLI exit codes for failed operations and invalid configuration.
- Add protocol and application fault-injection regression tests and isolate
  test fixtures from files in the developer's checkout.

## 1.0.0 - 2026-08-17

- MeshCore 2.3.7 client compatibility: `create_serial(port, baudrate=...)`,
  `create_tcp(host, port)`, `disconnect()`, contact resolution, and
  `start_auto_message_fetching()`.
- Binary-safe transport: file chunks are sent as `AZM1:` + base64 text so they
  survive companion `send_msg(dst, msg: str)` and stay distinct from chat.
- Inbound events read MeshCore `{pubkey_prefix, text}` (legacy Meshtastic-style
  `decoded.payload` is still accepted).
- Auto-reconnect is enabled on serial and TCP companion connections.
- Daemon auto-receive writes inbound files into `incoming_dir` using the
  advertised filename. Optional `allowed_senders` allowlist.
- Receive-side `max_file_size_bytes` enforcement (default 1 MiB). Set `0` to
  disable. Oversized advertised files are rejected before write. Directory
  extracts also refuse archives whose uncompressed size exceeds the cap.
- Stop-and-wait ACKs between DATA blocks, with timed retransmission of the
  last unacked packet.
- Invalid JSON configs are no longer overwritten with defaults.
- Legacy `mesh_packet_chunk_size` values above the encoded TXT_MSG limit are
  clamped instead of aborting startup.
- Packaged as version `1.0.0` (`pyproject.toml`, `--version`, console script).
- CI runs on Python 3.10 and 3.12 and fails on flake8 findings.

## Unreleased (folded into 1.0.0)

- Security: Prevent ZipSlip by validating zip entries before extraction in `akita_zmodem_meshcore.receive_directory()`; added `_safe_extract_zip()` to safely extract archives.
- Concurrency: Offloaded blocking MD5 checksum calculations to background threads with `asyncio.to_thread` to avoid blocking the event loop during send/receive operations.
- Reliability: Fixed `Sender` ACK handling in `zmodem.py` so the sender correctly updates and seeks to acknowledged offsets, improving resume/retransmit behaviour.
- Robustness: Added configuration validation and improved error handling for missing MeshCore client, invalid destinations, directory passed to `send_file`, and file-open failures during receive.
- Cleanup: Introduced global atexit cleanup for temporary zip files and added optional `cleanup` flags to directory send/receive methods.
- Security/maintenance: Iteration loops now copy transfer dict before modification to avoid runtime errors.
