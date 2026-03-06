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
    """Generator over complete payloads from *buffer*; leftover stays in buffer."""
    while True:
        if len(buffer) < 4:
            break
        length = struct.unpack("!I", buffer[:4])[0]
        if len(buffer) < 4 + length + 4:
            break
        start = 4
        end = 4 + length
        payload = bytes(buffer[start:end])
        crc_expected = struct.unpack("!I", buffer[end:end+4])[0]
        crc_actual = zlib.crc32(payload) & 0xFFFFFFFF
        if crc_actual != crc_expected:
            # drop corrupted packet and continue; log for diagnostics
            logging.debug("_deframe: CRC mismatch, dropping packet")
            del buffer[:4 + length + 4]
            continue
        yield payload
        del buffer[:4 + length + 4]


class Sender:
    def __init__(self, fobj, chunk_size: int = 256):
        self.fobj = fobj
        self.chunk_size = chunk_size
        self.filesize = os.fstat(fobj.fileno()).st_size
        self.filename = os.path.basename(fobj.name)
        self.offset = 0
        self._finished = False
        self._queue = []      # outgoing packet queue
        self._inbuf = bytearray()
        self.state = 'init'

    def is_finished(self):
        return self._finished

    def get_next_packet(self):
        if self._queue:
            return self._queue.pop(0)
        if self.state == 'init':
            # send START header
            payload = _START + struct.pack("!H", len(self.filename)) + self.filename.encode('utf-8')
            payload += struct.pack("!Q", self.filesize)
            self._queue.append(_frame(payload))
            self.state = 'waiting_ack'
            return self._queue.pop(0)
        if self.state == 'sending':
            # ensure file position matches current offset
            try:
                self.fobj.seek(self.offset)
            except Exception:
                pass
            data = self.fobj.read(self.chunk_size)
            if not data:
                self._queue.append(_frame(_END))
                self.state = 'finished'
                return self._queue.pop(0)
            payload = _DATA + struct.pack("!Q", self.offset) + data
            self.offset += len(data)
            return _frame(payload)
        if self.state == 'finished':
            self._finished = True
            return b""
        # in waiting_ack or other states, nothing to send until ack arrives
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
                off = struct.unpack("!Q", payload[1:9])[0]
                # remote acknowledges up to off; update our send offset to match
                # and position the file accordingly. Clamp to valid range.
                if off < 0:
                    off = 0
                if off > self.filesize:
                    off = self.filesize
                self.offset = off
                try:
                    self.fobj.seek(self.offset)
                except Exception:
                    pass
                self.state = 'sending'
            elif tp == _RESUME:
                off = struct.unpack("!Q", payload[1:9])[0]
                # Clamp resume offset to valid range before seeking
                if off < 0:
                    off = 0
                if off > self.filesize:
                    off = self.filesize
                self.offset = off
                try:
                    self.fobj.seek(off)
                except Exception:
                    pass
                self.state = 'sending'
            elif tp == _END:
                self.state = 'finished'
                self._finished = True
            # other control frames ignored
        return out


class Receiver:
    def __init__(self, fobj_or_path):
        """Accept either a file-like object or a filepath string.

        If a path is provided, the Receiver will open/close the file as
        appropriate during the transfer to support resume logic without the
        caller pre-opening the file (which could truncate it).
        """
        if isinstance(fobj_or_path, str):
            self.filepath = fobj_or_path
            self.fobj = None
        else:
            self.filepath = None
            self.fobj = fobj_or_path
        self._inbuf = bytearray()
        self._queue = []
        self.state = 'waiting'   # waiting for START header
        self.offset = 0
        self.expected_size = None

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
                name_len = struct.unpack("!H", payload[1:3])[0]
                name = payload[3:3+name_len].decode('utf-8')
                size = struct.unpack("!Q", payload[3+name_len:3+name_len+8])[0]
                self.expected_size = size
                # decide on resume
                # Determine existing size from filepath (if available) or
                # from the provided file object.  If the caller previously
                # opened the file with 'wb' we would have truncated it, so
                # prefer using a filepath and letting Receiver manage opens
                # to correctly support resume.
                existing = 0
                target_name = None
                if self.filepath:
                    target_name = self.filepath
                elif self.fobj and hasattr(self.fobj, 'name'):
                    target_name = self.fobj.name

                if target_name:
                    try:
                        existing = os.path.getsize(target_name)
                    except OSError:
                        existing = 0

                if existing and existing < size:
                    # resume: open for append
                    self.offset = existing
                    # close any previously opened handle
                    try:
                        if self.fobj:
                            try: self.fobj.close()
                            except Exception: pass
                    except Exception:
                        pass
                    self.fobj = open(target_name, 'ab') if target_name else None
                    resp = _RESUME + struct.pack("!Q", self.offset)
                else:
                    # start fresh: open for write (truncate)
                    try:
                        if self.fobj:
                            try: self.fobj.close()
                            except Exception: pass
                    except Exception:
                        pass
                    self.fobj = open(target_name, 'wb') if target_name else None
                    self.offset = 0
                    resp = _ACK + struct.pack("!Q", self.offset)
                out += _frame(resp)
                self.state = 'receiving'
            elif tp == _DATA and self.state == 'receiving':
                off = struct.unpack("!Q", payload[1:9])[0]
                chunk = payload[9:]
                if off != self.offset:
                    # out-of-order: request resume
                    resp = _RESUME + struct.pack("!Q", self.offset)
                    out += _frame(resp)
                else:
                    # Ensure we have an open file handle before writing
                    if self.fobj is None:
                        # attempt to open in append mode
                        try:
                            self.fobj = open(self.filepath, 'ab') if self.filepath else None
                        except Exception:
                            # cannot open file; request resume (no change)
                            resp = _RESUME + struct.pack("!Q", self.offset)
                            out += _frame(resp)
                            continue
                    self.fobj.write(chunk)
                    self.offset += len(chunk)
                    resp = _ACK + struct.pack("!Q", self.offset)
                    out += _frame(resp)
            elif tp == _END and self.state == 'receiving':
                self.state = 'done'
                try:
                    if self.fobj:
                        self.fobj.close()
                except Exception:
                    pass
                # final ack
                out += _frame(_END)
        return out
