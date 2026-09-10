"""Zmodem-like protocol implementation baked into the repo.

This module implements enough of the ZMODEM protocol to support
"true" behaviour for file transfers over the MeshCore link: header
exchange, checksum validation, block retransmission, and resumable
downloads.  It is *not* a line-for-line port of the full Zmodem
specification, but it provides a bi‑directional framed protocol that
behaves like Zmodem from the point of view of the surrounding code.

The public API mimics the previous third‑party library: ``Sender``
and ``Receiver`` objects expose ``get_next_packet()``, ``is_finished()``
and ``receive(data)``.  The latter method returns optional bytes which
should be sent back to the peer (control/ack frames).
"""

import os
import struct
import zlib
import logging

MAX_FRAME_PAYLOAD = 1024 * 1024

# packet types
_START = b'S'      # <filename_len:uint16><filename><filesize:uint64>
_DATA = b'D'       # <offset:uint64><payload>
_ACK = b'A'        # <offset:uint64>
_RESUME = b'R'     # <offset:uint64>
_END = b'E'

# framing helpers: length prefix (uint32) + payload + crc32


def _frame(payload: bytes) -> bytes:
    length = struct.pack("!I", len(payload))
    crc = struct.pack("!I", zlib.crc32(payload) & 0xFFFFFFFF)
    return length + payload + crc


def _deframe(buffer: bytearray):
    """Yield CRC-checked frames, recovering after lost or corrupt fragments.

    A retransmitted frame can follow an incomplete old frame in the stream.
    Search for a complete, valid frame before trusting an incomplete length.
    Retain incomplete input within the maximum frame size for the next call.
    """
    while len(buffer) >= 4:
        found = False
        for pos in range(len(buffer) - 3):
            length = struct.unpack_from("!I", buffer, pos)[0]
            if not 1 <= length <= MAX_FRAME_PAYLOAD:
                continue
            end = pos + 4 + length
            if end + 4 > len(buffer):
                continue
            payload = bytes(buffer[pos + 4:end])
            expected = struct.unpack_from("!I", buffer, end)[0]
            if zlib.crc32(payload) & 0xFFFFFFFF != expected:
                continue
            del buffer[:end + 4]
            yield payload
            found = True
            break
        if not found:
            if len(buffer) == 4 and struct.unpack("!I", buffer)[0] > MAX_FRAME_PAYLOAD:
                buffer.clear()
            elif len(buffer) > MAX_FRAME_PAYLOAD + 8:
                del buffer[:-(MAX_FRAME_PAYLOAD + 8)]
            break


def parse_start_header(data: bytes):
    """Return ``(filename, size)`` if *data* begins with a START frame.

    The 4-byte CRC at the end of the frame is not required; only the
    filename and filesize fields need to be present.
    """
    if not data or len(data) < 7:
        return None
    if data[4:5] != _START:
        return None
    frame_len = struct.unpack("!I", data[:4])[0]
    name_len = struct.unpack("!H", data[5:7])[0]
    if frame_len != 1 + 2 + name_len + 8:
        return None
    if len(data) < 7 + name_len + 8:
        return None
    try:
        filename = data[7:7 + name_len].decode("utf-8")
    except UnicodeDecodeError:
        return None
    size = struct.unpack("!Q", data[7 + name_len:7 + name_len + 8])[0]
    return filename, size


class Sender:
    def __init__(self, fobj, chunk_size: int = 256):
        if not isinstance(chunk_size, int) or not 1 <= chunk_size <= MAX_FRAME_PAYLOAD - 9:
            raise ValueError("chunk_size is outside the protocol payload limit")
        self.fobj = fobj
        self.chunk_size = chunk_size
        self.filesize = os.fstat(fobj.fileno()).st_size
        self.filename = os.path.basename(fobj.name)
        self.offset = 0
        self.acked_offset = 0
        self._finished = False
        self._queue = []      # outgoing packet queue
        self._inbuf = bytearray()
        self._last_packet = b""
        self.last_payload_bytes = 0
        self.state = 'init'

    def is_finished(self):
        return self._finished

    def peek_retransmit(self):
        """Last packet, if still waiting for an ACK/END confirmation."""
        if self.state in ('waiting_ack', 'waiting_end_ack') and self._last_packet:
            return self._last_packet
        return b""

    def _emit(self, packet, payload_bytes=0):
        self._last_packet = packet
        self.last_payload_bytes = payload_bytes
        return packet

    def get_next_packet(self):
        if self._queue:
            return self._emit(self._queue.pop(0))
        if self.state == 'init':
            # send START header
            encoded_name = self.filename.encode('utf-8')
            if len(encoded_name) > 65535:
                raise ValueError("filename is too long for protocol header")
            payload = (
                _START
                + struct.pack("!H", len(encoded_name))
                + encoded_name
            )
            payload += struct.pack("!Q", self.filesize)
            self.state = 'waiting_ack'
            return self._emit(_frame(payload))
        if self.state == 'sending':
            # ensure file position matches current offset
            try:
                self.fobj.seek(self.offset)
            except OSError as e:
                logging.warning(
                    "Sender: seek failed at offset %s: %s", self.offset, e)
                return b""
            data = self.fobj.read(self.chunk_size)
            if not data:
                self.state = 'waiting_end_ack'
                return self._emit(_frame(_END))
            payload = _DATA + struct.pack("!Q", self.offset) + data
            self.offset += len(data)
            self.state = 'waiting_ack'
            return self._emit(_frame(payload), len(data))
        if self.state in ('finished', 'waiting_end_ack', 'waiting_ack'):
            return b""
        return b""

    def receive(self, data: bytes):
        """Process incoming response from remote and return any packet to send."""
        if not data:
            return b""
        self._inbuf.extend(data)
        out = b""
        for payload in _deframe(self._inbuf):
            tp = payload[:1]
            if tp == _ACK:
                self._on_ack(payload)
            elif tp == _RESUME:
                self._on_resume(payload)
            elif payload == _END and self.state == 'waiting_end_ack':
                self.state = 'finished'
                self._finished = True
            # other control frames ignored
        return out

    def _seek_to(self, offset):
        try:
            self.fobj.seek(offset)
        except OSError as e:
            logging.warning("Sender: seek failed at offset %s: %s", offset, e)

    def _on_ack(self, payload):
        if len(payload) != 9 or self.state != 'waiting_ack':
            logging.debug("Sender.receive: short ACK frame ignored")
            return
        off = struct.unpack("!Q", payload[1:9])[0]
        # remote acknowledges up to off. Duplicate delivery is common
        # on MeshCore routes, so ignore anything older than the last
        # confirmed offset.
        if off != self.offset or off < self.acked_offset:
            logging.debug(
                "Sender.receive: stale ACK ignored (off=%d acked=%d)",
                off, self.acked_offset)
            return
        self.acked_offset = off
        if off > self.offset:
            self.offset = off
            self._seek_to(self.offset)
        self.state = 'sending'

    def _on_resume(self, payload):
        if len(payload) != 9 or self.state not in ('waiting_ack', 'waiting_end_ack'):
            logging.debug("Sender.receive: short RESUME frame ignored")
            return
        off = struct.unpack("!Q", payload[1:9])[0]
        # Clamp resume offset to valid range before seeking. Ignore
        # resume requests that predate already-acknowledged progress.
        if off > self.filesize:
            return
        if off < self.acked_offset:
            logging.debug(
                "Sender.receive: stale RESUME ignored (off=%d acked=%d)",
                off, self.acked_offset)
            return
        self.acked_offset = off
        self.offset = off
        self._seek_to(off)
        self.state = 'sending'


class Receiver:
    def __init__(self, fobj_or_path, max_file_size=None):
        """Accept either a file-like object or a filepath string.

        If a path is provided, the Receiver will open/close the file as
        appropriate during the transfer to support resume logic without the
        caller pre-opening the file (which could truncate it).
        """
        if isinstance(fobj_or_path, (str, os.PathLike)):
            self.filepath = os.fspath(fobj_or_path)
            self.fobj = None
        else:
            self.filepath = None
            self.fobj = fobj_or_path
        self._inbuf = bytearray()
        self._queue = []
        self.state = 'waiting'   # waiting for START header
        self.offset = 0
        self.expected_size = None
        self.filename = None
        self.max_file_size = max_file_size

    def is_finished(self):
        return self.state == 'done'

    def receive(self, data: bytes):
        """Feed incoming data; returns an ack/resume packet if appropriate."""
        if not data:
            return b""
        self._inbuf.extend(data)
        out = b""
        for payload in _deframe(self._inbuf):
            tp = payload[:1]
            if tp == _START:
                out += self._on_start(payload)
            elif tp == _DATA and self.state == 'receiving':
                out += self._on_data(payload)
            elif payload == _END and self.state == 'receiving':
                out += self._on_end()
            elif payload == _END and self.state == 'done':
                out += _frame(_END)
        return out

    def _target_name(self):
        if self.filepath:
            return self.filepath
        if self.fobj and hasattr(self.fobj, 'name'):
            return self.fobj.name
        return None

    def _close_fobj(self):
        if not self.fobj:
            return
        try:
            self.fobj.close()
        except Exception:
            pass
        self.fobj = None

    def _on_start(self, payload):
        if len(payload) < 11:
            logging.debug("Receiver.receive: short START frame ignored")
            return b""
        name_len = struct.unpack("!H", payload[1:3])[0]
        if len(payload) != 3 + name_len + 8:
            logging.debug("Receiver.receive: truncated START frame ignored")
            return b""
        try:
            filename = payload[3:3 + name_len].decode("utf-8")
        except UnicodeDecodeError:
            return b""
        size = struct.unpack("!Q",
                             payload[3 + name_len:3 + name_len + 8])[0]
        if self.max_file_size and size > self.max_file_size:
            raise ValueError("Advertised size exceeds max_file_size_bytes")
        if self.state != 'waiting':
            if filename == self.filename and size == self.expected_size:
                return _frame(_RESUME + struct.pack("!Q", self.offset))
            return b""
        self.filename = filename
        self.expected_size = size
        target_name = self._target_name()
        existing = 0
        if target_name:
            try:
                existing = os.path.getsize(target_name)
            except OSError:
                existing = 0

        if existing and existing < size:
            self.offset = existing
            if target_name:
                self._close_fobj()
                self.fobj = open(target_name, 'ab')
            elif self.fobj:
                try:
                    self.fobj.seek(self.offset)
                except OSError as e:
                    logging.warning("Receiver: seek failed: %s", e)
            self.state = 'receiving'
            return _frame(_RESUME + struct.pack("!Q", self.offset))

        if target_name:
            self._close_fobj()
            self.fobj = open(target_name, 'wb')
        elif self.fobj:
            try:
                self.fobj.seek(0)
                self.fobj.truncate(0)
            except OSError as e:
                logging.warning("Receiver: truncate failed: %s", e)
        self.offset = 0
        self.state = 'receiving'
        return _frame(_ACK + struct.pack("!Q", self.offset))

    def _on_data(self, payload):
        if len(payload) < 9:
            logging.debug("Receiver.receive: short DATA frame ignored")
            return b""
        off = struct.unpack("!Q", payload[1:9])[0]
        chunk = payload[9:]
        if self.expected_size is not None and off + len(chunk) > self.expected_size:
            return _frame(_RESUME + struct.pack("!Q", self.offset))
        if off != self.offset:
            return _frame(_RESUME + struct.pack("!Q", self.offset))
        if self.fobj is None:
            if not self.filepath:
                return _frame(_RESUME + struct.pack("!Q", self.offset))
            try:
                self.fobj = open(self.filepath, 'ab')
            except OSError:
                return _frame(_RESUME + struct.pack("!Q", self.offset))
        self.fobj.write(chunk)
        self.fobj.flush()
        self.offset += len(chunk)
        return _frame(_ACK + struct.pack("!Q", self.offset))

    def _on_end(self):
        if self.expected_size is not None and self.offset != self.expected_size:
            return _frame(_RESUME + struct.pack("!Q", self.offset))
        self.state = 'done'
        self._close_fobj()
        return _frame(_END)
