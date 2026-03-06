# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

- Security: Prevent ZipSlip by validating zip entries before extraction in `akita_zmodem_meshcore.receive_directory()`; added `_safe_extract_zip()` to safely extract archives.
- Concurrency: Offloaded blocking MD5 checksum calculations to background threads with `asyncio.to_thread` to avoid blocking the event loop during send/receive operations.
- Reliability: Fixed `Sender` ACK handling in `zmodem.py` so the sender correctly updates and seeks to acknowledged offsets, improving resume/retransmit behaviour.
- Robustness: Added configuration validation and improved error handling for missing MeshCore client, invalid destinations, directory passed to `send_file`, and file-open failures during receive.
- Cleanup: Introduced global atexit cleanup for temporary zip files and added optional `cleanup` flags to directory send/receive methods.
- Security/maintenance: Iteration loops now copy transfer dict before modification to avoid runtime errors.

(See commit history for full details.)
