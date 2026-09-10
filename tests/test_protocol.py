import struct
import pytest

import zmodem as zm


def test_parse_start_header_reads_name_and_size(tmp_path):
    src = tmp_path / "notes.bin"
    src.write_bytes(b"abc")
    with open(src, "rb") as sf:
        sender = zm.Sender(sf, chunk_size=2)
        start = sender.get_next_packet()
    info = zm.parse_start_header(start)
    assert info == ("notes.bin", 3)


def test_sender_waits_for_ack_between_data_blocks(tmp_path):
    src = tmp_path / "blocks.bin"
    src.write_bytes(b"0123456789")
    with open(src, "rb") as sf:
        sender = zm.Sender(sf, chunk_size=4)
        sender.get_next_packet()
        assert sender.state == "waiting_ack"
        assert sender.get_next_packet() == b""

        sender.receive(zm._frame(zm._ACK + struct.pack("!Q", 0)))
        first = sender.get_next_packet()
        assert first
        assert sender.state == "waiting_ack"
        assert sender.offset == 4
        assert sender.get_next_packet() == b""
        assert sender.peek_retransmit() == first

        sender.receive(zm._frame(zm._ACK + struct.pack("!Q", 4)))
        second = sender.get_next_packet()
        assert second
        assert sender.offset == 8


def test_receiver_exposes_filename_from_start(tmp_path):
    src = tmp_path / "cafe.bin"
    src.write_bytes(b"payload")
    dst = tmp_path / "out.bin"
    with open(src, "rb") as sf:
        sender = zm.Sender(sf, chunk_size=8)
        receiver = zm.Receiver(str(dst))
        start = sender.get_next_packet()
        receiver.receive(start)
    assert receiver.filename == "cafe.bin"
    assert receiver.expected_size == 7
    assert receiver.state == "receiving"
    receiver._close_fobj()


def test_duplicate_ack_does_not_skip_unacknowledged_block(tmp_path):
    src = tmp_path / 'source'
    src.write_bytes(b'0123456789')
    with src.open('rb') as f:
        sender = zm.Sender(f, 4)
        sender.get_next_packet()
        ack = zm._frame(zm._ACK + struct.pack('!Q', 0))
        sender.receive(ack)
        packet = sender.get_next_packet()
        sender.receive(ack)
        assert sender.get_next_packet() == b''
        assert sender.peek_retransmit() == packet
        sender.receive(zm._frame(zm._ACK + struct.pack('!Q', 999)))
        assert sender.acked_offset == 0


def test_sender_ignores_premature_end(tmp_path):
    src = tmp_path / 'source'
    src.write_bytes(b'0123')
    with src.open('rb') as f:
        sender = zm.Sender(f)
        sender.get_next_packet()
        sender.receive(zm._frame(zm._END))
        assert not sender.is_finished()


def test_duplicate_start_and_end_preserve_completed_file(tmp_path):
    src = tmp_path / 'source'
    src.write_bytes(b'0123')
    dst = tmp_path / 'target'
    receiver = zm.Receiver(dst)
    with src.open('rb') as f:
        sender = zm.Sender(f)
        start = sender.get_next_packet()
        sender.receive(receiver.receive(start))
        sender.receive(receiver.receive(sender.get_next_packet()))
        receiver.receive(start)
        assert receiver.offset == 4
        end = sender.get_next_packet()
        receiver.receive(end)  # Simulate losing the first END response.
        sender.receive(receiver.receive(end))
        assert sender.is_finished()
        receiver.receive(start)
        assert receiver.is_finished()
    assert dst.read_bytes() == b'0123'


def test_receiver_enforces_sizes_before_writing(tmp_path):
    dst = tmp_path / 'target'
    start = zm._frame(b'S' + struct.pack('!H', 1) + b'x' + struct.pack('!Q', 4))
    receiver = zm.Receiver(dst, max_file_size=3)
    with pytest.raises(ValueError):
        receiver.receive(start)
    assert not dst.exists()
    receiver = zm.Receiver(dst)
    receiver.receive(start)
    receiver.receive(zm._frame(b'D' + struct.pack('!Q', 0) + b'oversized'))
    assert receiver.offset == 0
    assert dst.read_bytes() == b''
    receiver._close_fobj()


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'corrupt'])
def test_deframe_recovers_retransmitted_fragmented_packet(damage):
    packet = zm._frame(b'D' + struct.pack('!Q', 0) + bytes(range(256)))
    pieces = [packet[i:i + 64] for i in range(0, len(packet), 64)]
    if damage == 'missing':
        del pieces[1]
    elif damage == 'duplicate':
        pieces.insert(1, pieces[1])
    else:
        pieces[1] = b'X' * len(pieces[1])
    buffer = bytearray()
    decoded = []
    for piece in pieces + [packet[i:i + 64] for i in range(0, len(packet), 64)]:
        buffer.extend(piece)
        decoded.extend(zm._deframe(buffer))
    assert decoded == [packet[4:-4]]


def test_deframe_does_not_parse_frames_embedded_in_file_content():
    embedded = zm._frame(b'E')
    packet = zm._frame(b'D' + struct.pack('!Q', 0) + embedded + b'x' * 200)
    buffer = bytearray(packet[:64])
    assert list(zm._deframe(buffer)) == []
    buffer.extend(packet[64:])
    assert list(zm._deframe(buffer)) == [packet[4:-4]]
