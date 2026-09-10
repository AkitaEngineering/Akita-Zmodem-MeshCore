"""MeshCore 2.3.7 transport helpers.

Companion ``send_msg`` takes a UTF-8 text body, not raw radio bytes. File
chunks are therefore framed as ``AZM1:`` plus base64 so they survive that
API and stay distinguishable from ordinary chat.

MeshCore TXT_MSG radio payloads are at most 184 bytes. Timestamp (4) and
the extra/flags byte (1) leave 179 characters for text.
"""

import base64
import os
import struct

AZM_PREFIX = "AZM1:"
MESHCORE_MAX_PACKET_PAYLOAD = 184
MESHCORE_TXT_OVERHEAD = 5  # uint32 timestamp + extra byte
MESHCORE_MAX_TEXT_CHARS = MESHCORE_MAX_PACKET_PAYLOAD - MESHCORE_TXT_OVERHEAD
APP_PORT_HEADER_FORMAT = "!H"
APP_PORT_HEADER_SIZE = struct.calcsize(APP_PORT_HEADER_FORMAT)


def max_binary_chunk_size(max_text=MESHCORE_MAX_TEXT_CHARS, prefix=AZM_PREFIX):
    """Largest binary blob whose encoded form fits in a MeshCore text body."""
    usable = max_text - len(prefix)
    if usable < 4:
        return 0
    return (usable // 4) * 3


MESHCORE_MAX_BINARY_CHUNK = max_binary_chunk_size()


def encoded_length(n_binary, prefix=AZM_PREFIX):
    return len(prefix) + 4 * ((n_binary + 2) // 3)


def encode_mesh_message(chunk: bytes) -> str:
    if not isinstance(chunk, (bytes, bytearray)):
        raise TypeError("chunk must be bytes")
    return AZM_PREFIX + base64.b64encode(bytes(chunk)).decode("ascii")


def decode_mesh_message(text):
    if not isinstance(text, str) or not text.startswith(AZM_PREFIX):
        return None
    try:
        return base64.b64decode(text[len(AZM_PREFIX):], validate=True)
    except (ValueError, TypeError):
        return None


def parse_inbound_event(event):
    """Normalize a MeshCore (or legacy) inbound event into {source, data}.

    ``data`` is the binary mesh chunk, including the 2-byte app-port header
    when the peer used this transport.
    """
    payload = getattr(event, "payload", event)
    if not isinstance(payload, dict):
        return None

    src = (
        payload.get("pubkey_prefix")
        or payload.get("from")
        or payload.get("from_num")
        or "unknown"
    )
    src = str(src)

    text = payload.get("text")
    if isinstance(text, str):
        data = decode_mesh_message(text)
        if data:
            return {"source": src, "data": data}

    decoded = payload.get("decoded") or {}
    raw = decoded.get("payload") if isinstance(decoded, dict) else None
    if isinstance(raw, (bytes, bytearray)) and raw:
        return {"source": src, "data": bytes(raw)}
    if isinstance(text, (bytes, bytearray)) and text:
        return {"source": src, "data": bytes(text)}
    return None


def is_command_error(result, error_type):
    if result is None:
        return False
    result_type = getattr(result, "type", None)
    if result_type is None:
        return False
    return result_type == error_type


def resolve_destination(mesh, dest):
    """Map a name, ``!``-prefixed id, or key prefix to a MeshCore contact."""
    if not dest or mesh is None:
        return dest
    getter = getattr(mesh, "get_contact_by_name", None)
    if callable(getter):
        contact = getter(dest)
        if contact:
            return contact
    key = dest[1:] if isinstance(dest, str) and dest.startswith("!") else dest
    prefix_getter = getattr(mesh, "get_contact_by_key_prefix", None)
    if callable(prefix_getter) and key:
        contact = prefix_getter(key)
        if contact:
            return contact
    return dest


def sanitize_filename(name):
    if not name:
        return None
    name = os.path.basename(str(name).replace("\\", "/")).strip()
    if not name or name in (".", "..") or "\x00" in name:
        return None
    return name


def peer_matches(stored, src):
    """True if an inbound pubkey prefix/name refers to *stored* destination."""
    if stored is None or src is None:
        return False
    src = str(src).lstrip("!").lower()
    if not src:
        return False
    candidates = []
    if isinstance(stored, dict):
        for key in ("public_key", "public_key_hex", "adv_name", "name"):
            val = stored.get(key)
            if val:
                candidates.append(str(val))
    else:
        candidates.append(str(stored))
    for token in candidates:
        token = token.lstrip("!").lower()
        if token and (src == token or src.startswith(token)
                      or token.startswith(src)):
            return True
    return False


def sender_is_allowed(src, allowed_senders):
    if not allowed_senders:
        return True
    src = str(src).lstrip("!").lower()
    if not src:
        return False
    for allowed in allowed_senders:
        token = str(allowed).lstrip("!").lower()
        if not token:
            continue
        if src == token or src.startswith(token) or token.startswith(src):
            return True
    return False
