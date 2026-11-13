from __future__ import annotations

import base64
import binascii
import hashlib
import unittest

from synoarr.bencode import BencodeError, bdecode, bencode, magnet_info, torrent_info


def make_torrent(name="Night.of.the.Living.Dead.1968.1080p.BluRay.mkv", extra_info=None):
    info = {
        "name": name,
        "piece length": 262144,
        "pieces": hashlib.sha1(b"x").digest() + hashlib.sha1(b"y").digest(),
        "length": 123456789,
    }
    if extra_info:
        info.update(extra_info)
    meta = {"announce": "http://tracker.example/announce", "creation date": 1700000000, "info": info}
    return bencode(meta), info


class BencodeTests(unittest.TestCase):
    def test_roundtrip(self):
        value = {b"a": 1, b"b": [b"x", -3, {b"k": b""}], b"z": b"\x00\xff"}
        self.assertEqual(bdecode(bencode(value)), value)

    def test_known_encoding(self):
        self.assertEqual(bencode({"spam": ["a", 42], "cow": "moo"}), b"d3:cow3:moo4:spaml1:ai42eee")
        self.assertEqual(bdecode(b"i-42e"), -42)
        self.assertEqual(bdecode(b"0:"), b"")
        self.assertEqual(bdecode(b"le"), [])

    def test_invalid(self):
        for bad in (b"", b"i12", b"iabce", b"5:abc", b"l1:a", b"d1:ai1e", b"x", b"<html>", b"i1ei2e", b"di1ei2ee"):
            with self.assertRaises(BencodeError, msg=bad):
                bdecode(bad)
        self.assertTrue(issubclass(BencodeError, ValueError))

    def test_deep_nesting_raises_value_error(self):
        with self.assertRaises(ValueError):
            bdecode(b"l" * 5000 + b"e" * 5000)

    def test_trailing_whitespace_tolerated(self):
        self.assertEqual(bdecode(b"i1e\n"), 1)


class TorrentInfoTests(unittest.TestCase):
    def test_name_and_hash(self):
        data, info = make_torrent()
        name, info_hash = torrent_info(data)
        self.assertEqual(name, "Night.of.the.Living.Dead.1968.1080p.BluRay.mkv")
        self.assertEqual(info_hash, hashlib.sha1(bencode(info)).hexdigest())
        self.assertEqual(len(info_hash), 40)

    def test_accented_name(self):
        data, _ = make_torrent(name="Le Fabuleux Destin d'Amélie Poulain (2001)")
        self.assertEqual(torrent_info(data)[0], "Le Fabuleux Destin d'Amélie Poulain (2001)")

    def test_name_utf8_preferred(self):
        data, _ = make_torrent(name=b"Am\xe9lie", extra_info={"name.utf-8": "Amélie"})
        self.assertEqual(torrent_info(data)[0], "Amélie")

    def test_latin1_name_does_not_crash(self):
        data, _ = make_torrent(name=b"Am\xe9lie")
        self.assertEqual(torrent_info(data)[0], "Amélie")

    def test_hash_uses_original_bytes_for_non_canonical_info(self):
        # keys not sorted: re-encoding would change the hash, the original bytes must be hashed
        raw_info = b"d6:lengthi5e4:name3:abc12:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"e"
        raw_info_unsorted = b"d4:name3:abc6:lengthi5e12:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"e"
        data = b"d8:announce3:foo4:info" + raw_info_unsorted + b"e"
        name, h = torrent_info(data)
        self.assertEqual(name, "abc")
        self.assertEqual(h, hashlib.sha1(raw_info_unsorted).hexdigest())
        self.assertNotEqual(h, hashlib.sha1(raw_info).hexdigest())

    def test_trailing_garbage_tolerated(self):
        data, info = make_torrent()
        self.assertEqual(torrent_info(data + b"\r\n")[1], hashlib.sha1(bencode(info)).hexdigest())

    def test_html_page_raises(self):
        with self.assertRaises(ValueError):
            torrent_info(b"<!DOCTYPE html><html><body>Login required</body></html>")

    def test_invalid_inputs_raise(self):
        for bad in (b"", b"d", b"d4:infoi1ee", b"d8:announce3:fooe", b"d4:info", b"le", b"d3:foo"):
            with self.assertRaises(ValueError, msg=bad):
                torrent_info(bad)


class MagnetTests(unittest.TestCase):
    HEX = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"

    def test_hex(self):
        m = "magnet:?xt=urn:btih:%s&dn=Night+of+the+Living+Dead+%%281968%%29&tr=udp%%3A%%2F%%2Ft" % self.HEX.upper()
        self.assertEqual(magnet_info(m), ("Night of the Living Dead (1968)", self.HEX))

    def test_base32(self):
        b32 = base64.b32encode(binascii.unhexlify(self.HEX)).decode("ascii")
        self.assertEqual(len(b32), 32)
        self.assertEqual(magnet_info("magnet:?xt=urn:btih:%s" % b32), (None, self.HEX))
        self.assertEqual(magnet_info("magnet:?xt=urn:btih:%s&dn=x" % b32.lower()), ("x", self.HEX))

    def test_btmh_and_btih(self):
        m = "magnet:?xt=urn:btmh:1220abcdef&xt=urn:btih:%s&dn=Hybrid" % self.HEX
        self.assertEqual(magnet_info(m), ("Hybrid", self.HEX))

    def test_not_magnet(self):
        self.assertEqual(magnet_info("http://example.com/file.torrent"), (None, None))
        self.assertEqual(magnet_info(""), (None, None))
        self.assertEqual(magnet_info(None), (None, None))  # type: ignore[arg-type]

    def test_invalid_hash(self):
        self.assertEqual(magnet_info("magnet:?xt=urn:btih:zzzz&dn=Foo"), ("Foo", None))


if __name__ == "__main__":
    unittest.main()
