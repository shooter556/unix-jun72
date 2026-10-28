#!/usr/bin/env python3
"""
Build a Unix V1 initialization DECtape on the host, in the same layout
that /usr/sys/maki.s writes to /dev/tap7:

  block 0       vcboot (taken from the original 1972 /usr/sys/a.out)
  blocks 1-64   the top 64 blocks of the RF disk (bos, warm, cold, unassigned)
  block 65 on   files: a metadata block (size, mode, uid, name) followed by
                data blocks; a metadata block with size 0 ends the list

Run it from the top of the unix-jun72 checkout.
"""

import argparse
import os
import re
import struct
import sys

BLK = 512
RF_TOP = 960            # first block of the reserved area (octal 1700)
COLD_BLK = 988          # cold UNIX slot (octal 1734)
SLOT_BYTES = 6 * 1024 * 2   # bos loads 6K words per kernel slot

# The nine files maki.s puts on the tape, with mode and uid taken from
# the s2 listing in fs/Readme (x=exec 020, u=setuid+exec 060, rwr- = 016).
FILES = [
    ("/etc/init",  0o36, 1),
    ("/etc/getty", 0o36, 3),
    ("/bin/chmod", 0o36, 3),
    ("/bin/date",  0o76, 0),
    ("/bin/login", 0o76, 0),
    ("/bin/mkdir", 0o76, 0),
    ("/bin/sh",    0o36, 1),
    ("/bin/tap",   0o76, 0),
    ("/bin/ls",    0o36, 3),
]


def pad(data):
    """Pad a byte string to a whole number of 512-byte blocks."""
    return data + b"\0" * (-len(data) % BLK)


def words(data, n):
    """Return the first n little-endian 16-bit words of data."""
    return struct.unpack("<%dH" % n, data[:2 * n])


def get_vcboot(maki_aout):
    """Extract the 512 bytes maki writes to block 0, starting at vcboot."""
    d = open(maki_aout, "rb").read()
    magic, tsize, dsize = words(d, 3)
    if magic != 0o407:
        sys.exit("%s: not an 0407 a.out" % maki_aout)
    text = d[16:16 + tsize + dsize]
    # vcboot starts with "mov $20000,sp" = 012706 020000
    sig = struct.pack("<HH", 0o12706, 0o20000)
    off = text.find(sig)
    if off < 0 or off % 2:
        sys.exit("vcboot not found in %s" % maki_aout)
    return pad(text[off:off + BLK])[:BLK]


def get_rf_top(rf_image, cold_aout):
    """Read RF blocks 960-1023; optionally replace the cold slot."""
    d = open(rf_image, "rb").read()
    d = d + b"\0" * max(0, 1024 * BLK - len(d))   # short image = zero fill
    top = bytearray(d[RF_TOP * BLK:1024 * BLK])
    if cold_aout:
        k = open(cold_aout, "rb").read()
        if words(k, 1)[0] != 0o407:
            sys.exit("%s: not an 0407 a.out" % cold_aout)
        image = k[16:16 + SLOT_BYTES]          # strip header, keep 6K words
        start = (COLD_BLK - RF_TOP) * BLK
        top[start:start + SLOT_BYTES] = image.ljust(SLOT_BYTES, b"\0")
    # Sanity checks: kernels start with the trap vectors 4;4
    for name, blk in (("warm", 964), ("cold", COLD_BLK)):
        off = (blk - RF_TOP) * BLK
        if words(top[off:], 2) != (4, 4):
            sys.exit("%s UNIX slot (block %d) does not look like a kernel"
                     % (name, blk))
    if top[(964 - RF_TOP) * BLK:][:SLOT_BYTES] == \
       top[(COLD_BLK - RF_TOP) * BLK:][:SLOT_BYTES]:
        sys.exit("cold slot is identical to warm slot: no cold kernel?")
    return bytes(top)


def readme_perms(readme):
    """Map path -> (mode, uid) from the s2 listing in fs/Readme."""
    perms = {}
    pat = re.compile(r"^([-xu])([-r])([-w])([-r])([-w])\s+(\d+)\s+\d+\s+.*\s(/\S+)\s+\d+\s*$")
    for line in open(readme, encoding="latin-1"):
        m = pat.match(line)
        if not m:
            continue
        x, ur, uw, orr, ow, uid, path = m.groups()
        mode = {"u": 0o60, "x": 0o20, "-": 0}[x]
        mode |= (ur == "r") * 0o10 | (uw == "w") * 0o4
        mode |= (orr == "r") * 0o2 | (ow == "w") * 0o1
        perms[path] = (mode, int(uid))
    return perms


def all_files(root, perms, skip):
    """Every regular file under root/{bin,etc,tmp}, minus those in skip."""
    out = []
    for top in ("bin", "etc", "tmp"):
        for name in sorted(os.listdir(os.path.join(root, top))):
            path = "/%s/%s" % (top, name)
            if path in skip or not os.path.isfile(root + path):
                continue
            # Unknown files get rw for owner, r for others, uid 1
            mode, uid = perms.get(path, (0o16, 1))
            out.append((path, mode, uid))
    return out


def file_entry(root, path, mode, uid):
    """One file in maki format: metadata block, then data blocks."""
    data = open(root + path, "rb").read()
    if len(data) > 0xFFFF:
        sys.exit("%s too large" % path)
    meta = struct.pack("<HBB", len(data), mode, uid) + path.encode() + b"\0"
    return pad(meta) + pad(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rf", default="images/rf0.dsk",
                    help="RF image to copy bos/warm/cold from")
    ap.add_argument("--cold", help="cold kernel a.out to put in the cold slot")
    ap.add_argument("--maki", default="fs/usr/sys/a.out",
                    help="original assembled maki (source of vcboot)")
    ap.add_argument("--root", default="build/root",
                    help="directory holding the root file tree")
    ap.add_argument("--all", action="store_true",
                    help="also put every other file of /bin, /etc, /tmp on the tape")
    ap.add_argument("--readme", default="fs/Readme",
                    help="s2 listing used for modes/uids with --all")
    ap.add_argument("-o", "--output", default="images/inittape")
    a = ap.parse_args()

    tape = get_vcboot(a.maki) + get_rf_top(a.rf, a.cold)
    assert len(tape) == 65 * BLK
    files = list(FILES)
    if a.all:
        files += all_files(a.root, readme_perms(a.readme),
                           set(p for p, _, _ in FILES))
    for path, mode, uid in files:
        tape += file_entry(a.root, path, mode, uid)
    if len(tape) // BLK > 578:
        sys.exit("tape too long: %d blocks (DECtape holds 578)" % (len(tape) // BLK))
    tape += pad(struct.pack("<H", 0))            # end-of-list marker
    open(a.output, "wb").write(tape)
    print("wrote %s: %d blocks" % (a.output, len(tape) // BLK))


if __name__ == "__main__":
    main()
