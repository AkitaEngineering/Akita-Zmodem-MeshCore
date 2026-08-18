import types

from mesh_transport import (
    AZM_PREFIX,
    MESHCORE_MAX_BINARY_CHUNK,
    MESHCORE_MAX_TEXT_CHARS,
    decode_mesh_message,
    encode_mesh_message,
    encoded_length,
    parse_inbound_event,
    peer_matches,
    resolve_destination,
    sanitize_filename,
    sender_is_allowed,
)


def test_encode_decode_roundtrip():
    payload = b"\x00\x01binary\xff\xfe"
    text = encode_mesh_message(payload)
    assert text.startswith(AZM_PREFIX)
    assert decode_mesh_message(text) == payload
    assert encoded_length(len(payload)) == len(text)
    assert len(text) <= MESHCORE_MAX_TEXT_CHARS or len(payload) > (
        MESHCORE_MAX_BINARY_CHUNK)


def test_max_binary_chunk_fits_text_body():
    chunk = b"\xab" * MESHCORE_MAX_BINARY_CHUNK
    assert len(encode_mesh_message(chunk)) <= MESHCORE_MAX_TEXT_CHARS


def test_decode_rejects_plain_chat():
    assert decode_mesh_message("hello from a radio") is None
    assert decode_mesh_message(AZM_PREFIX + "!!!!") is None


def test_parse_inbound_event_meshcore_text():
    text = encode_mesh_message(b"\x07\xd1payload")
    event = types.SimpleNamespace(
        payload={"pubkey_prefix": "aabbcc", "text": text})
    parsed = parse_inbound_event(event)
    assert parsed == {"source": "aabbcc", "data": b"\x07\xd1payload"}


def test_parse_inbound_event_legacy_decoded_payload():
    event = types.SimpleNamespace(
        payload={
            "from_num": 12,
            "decoded": {"payload": b"\x00\x01raw"},
        })
    parsed = parse_inbound_event(event)
    assert parsed == {"source": "12", "data": b"\x00\x01raw"}


def test_parse_inbound_event_ignores_unrelated_chat():
    event = types.SimpleNamespace(
        payload={"pubkey_prefix": "aabbcc", "text": "weather later?"})
    assert parse_inbound_event(event) is None


def test_resolve_destination_prefers_name_then_prefix():
    class Mesh:
        def get_contact_by_name(self, name):
            if name == "Alice":
                return {"public_key": "aa" * 32, "adv_name": "Alice"}
            return None

        def get_contact_by_key_prefix(self, prefix):
            if prefix.startswith("dead"):
                return {"public_key": "deadbeef"}
            return None

    mesh = Mesh()
    assert resolve_destination(mesh, "Alice")["adv_name"] == "Alice"
    assert resolve_destination(mesh, "!deadbeef")["public_key"] == "deadbeef"
    assert resolve_destination(mesh, "unknown") == "unknown"


def test_peer_matches_public_key_prefix():
    contact = {"public_key": "aabbccddeeff0011", "adv_name": "Alice"}
    assert peer_matches(contact, "aabbcc")
    assert peer_matches("Alice", "alice")
    assert not peer_matches(contact, "ffff")


def test_sanitize_filename_blocks_paths():
    assert sanitize_filename("../etc/passwd") == "passwd"
    assert sanitize_filename("ok.txt") == "ok.txt"
    assert sanitize_filename("..") is None
    assert sanitize_filename("") is None


def test_sender_allowlist():
    assert sender_is_allowed("aabbcc", [])
    assert sender_is_allowed("aabbccdd", ["aabb"])
    assert not sender_is_allowed("ffff", ["aabb"])
