import struct

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
