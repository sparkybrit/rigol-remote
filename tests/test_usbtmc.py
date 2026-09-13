import struct

import pytest
from conftest import FakeRigol, block

from rigol_remote.usbtmc import UsbtmcError, UsbtmcTransport

IDN = "RIGOL TECHNOLOGIES,DS1054Z,DS1ZA224210859,00.04.04.SP4\n"


def test_command_frame_is_dev_dep_msg_out_padded_to_four_bytes():
    fake = FakeRigol()
    written = []
    fake.write = written.append
    UsbtmcTransport(fake).write("*IDN?")
    (frame,) = written
    assert struct.unpack_from("<BBBxIB", frame) == (1, 1, 0xFE, 6, 1)
    assert frame[12:] == b"*IDN?\n\0\0"


def test_reply_longer_than_one_packet_is_not_truncated():
    # Plain read() on the kernel driver stops at 52 bytes here ("...00.04.04.S").
    t = UsbtmcTransport(FakeRigol({"*IDN?": IDN}))
    assert t.query("*IDN?") == IDN.rstrip("\n")


def test_multi_transfer_reply_with_zero_length_packet():
    # 12 + 500 = 512 bytes: the first transfer fills 8 packets exactly, so a ZLP follows it.
    waveform = block(bytes(range(200)) * 6)
    fake = FakeRigol({":WAV:DATA?": waveform})
    assert UsbtmcTransport(fake).query_bytes(":WAV:DATA?") == waveform
    assert not fake.packets


def test_final_transfer_ending_on_packet_boundary():
    reply = b"x" * (500 + 52)  # second transfer is 12 + 52 = 64 bytes: one full packet, then a ZLP
    fake = FakeRigol({"Q?": reply})
    assert UsbtmcTransport(fake).query_bytes("Q?") == reply
    assert not fake.packets


def test_odd_length_reply_drops_pad_byte():
    t = UsbtmcTransport(FakeRigol({":SYST:ERR?": '0,"No error"\n'}))
    assert t.query(":SYST:ERR?") == '0,"No error"'


def test_consecutive_queries_stay_in_sync():
    t = UsbtmcTransport(FakeRigol({"A?": "1\n", "B?": block(b"z" * 1000), "C?": "3\n"}))
    assert [t.query("A?"), len(t.query_bytes("B?")), t.query("C?")] == ["1", 1012, "3"]


def test_btag_wraps_from_255_to_1():
    t = UsbtmcTransport(FakeRigol({"A?": "1\n"}))
    for _ in range(300):
        assert t.query("A?") == "1"


def test_wrong_btag_is_an_error():
    fake = FakeRigol({"A?": "1\n"})
    fake.tag_offset = 1
    with pytest.raises(UsbtmcError, match="reply header"):
        UsbtmcTransport(fake).query("A?")


def test_no_reply_times_out():
    with pytest.raises(TimeoutError):
        UsbtmcTransport(FakeRigol()).query(":NOPE?")


def test_transfer_cut_short_by_zero_length_packet_is_an_error():
    fake = FakeRigol({"A?": IDN})
    t = UsbtmcTransport(fake)
    t.write("A?")
    real_write = fake.write

    def write_then_end_early(data):
        real_write(data)
        fake.packets[-1] = b""

    fake.write = write_then_end_early
    with pytest.raises(UsbtmcError, match="transfer ended"):
        t.read()


def test_transfer_that_stops_mid_reply_times_out():
    fake = FakeRigol({"A?": IDN})
    t = UsbtmcTransport(fake)
    t.write("A?")
    real_write = fake.write

    def write_then_lose_last_packet(data):
        real_write(data)
        fake.packets.pop()

    fake.write = write_then_lose_last_packet
    with pytest.raises(TimeoutError):
        t.read()


def test_drain_discards_leftovers():
    fake = FakeRigol()
    fake.packets.extend([b"P4\n]", b"", b"stale"])
    assert UsbtmcTransport(fake).drain() == 9
    assert not fake.packets
