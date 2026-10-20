#!/usr/bin/env python3
"""Raise the DIYLESS Thermostat 3 PID "integral floor" by LOCATING the code to patch,
instead of using fixed addresses — so it keeps working on firmware versions that moved
things around (see patch_fw.py for the fast, fixed-offset version for one exact build).

WHAT "THE FLOOR" IS
  The thermostat runs a PID controller whose output is the boiler water-temperature
  setpoint. That output is clamped to a minimum ("floor"), hard-coded to 10 in stock.
  We raise it to N (default 35). But the firmware ALSO turns the boiler off when the
  setpoint drops to that same 10, so we must move the off-threshold too, or the boiler
  short-cycles. This tool changes both, consistently.

HOW IT FINDS THEM (signature scanning)
  Addresses change between builds, but the *shape* of the code and the memory layout of
  the controller object don't. So we recognize a few machine instructions and follow
  references:
    - the "bounds setter": a tiny function that stores its 2nd arg to object+8 (the floor)
      and 3rd arg to object+9 (the ceiling). We find it, then look at everyone who CALLS
      it: each call sets the floor via `movi a11, 10` — those are the sites to patch.
    - the "off threshold": code that builds/loads the constant 10.0 and compares it to the
      setpoint (two compiler forms — see threshold_candidates); we rewrite it to read the
      floor value instead of a fixed 10.
  If anything is ambiguous we ABORT rather than risk patching the wrong instruction
  (this drives a boiler). Then we fix the image checksum/hash and re-check our work.

XTENSA 101 (just enough to read the decoders below)
  - Instructions are 2 or 3 bytes. Rule: 2 bytes if (first_byte & 0xF) >= 8, else 3.
  - "registers" a0..a15 hold values; function arguments arrive in a2, a3, a4, ...
  - an object pointer ("this") sits in a register; a field is accessed as [reg + offset].
  - the instructions we care about:
      movi  aX, k        load constant k into register aX      (here: the floor value)
      s8i   aX, aB, off  store low byte of aX to [aB+off]       (setter writes floor/ceiling)
      l8ui  aX, aB, off  load  byte from [aB+off] into aX
      call  target       call a function
      lsi   fX, aB, off  load a float from [aB+off] into FP reg fX   (the setpoint)
      wfr   fX, aY       move integer reg aY into FP reg fX
      ufloat.s fX, aY    convert integer in aY to float in fX
      olt.s  b, fX, fY   set bool b = (fX < fY)                 (the threshold compare)

    python patch_fw_auto.py in.bin out.bin [--floor 35] [-v]
  in.bin = an app image (DIYLESS download) OR a full flash backup (read-flash 0 ALL); for a
  backup we take the app slot the thermostat actually boots, per its otadata record.
"""
import argparse
import hashlib
import struct
import sys
import zlib
from typing import NamedTuple, Optional, List

# ---- tunables (all heuristics in one place) --------------------------------
FLOOR_ARG_RANGE   = (11, 79)    # allowed --floor: above the off-level (10), below the ceiling (80)
CALLER_FLOOR_RANGE = (1, 95)    # a plausible floor constant seen in a caller (movi a11, k)
PIDOFF_RANGE      = (0x10, 0x400)   # plausible offset of the PID sub-object inside the controller
FLOOR_SCAN_BACK   = 0x40        # how far before a call to look for its `movi a11, <floor>`
PIDOFF_SCAN_BACK  = 0x12        # the `movi a10, <offset>` arg-1 setup sits right before the call
ENTRY_SCAN_BACK   = 0x20        # how far back a function's `entry` opcode can be
C10_MOVI_IMM      = 0x209       # `movi aS, 0x209; slli aS, aS, 21` builds the float 10.0 (0x41200000)
C10_FLOAT         = 0x41200000  # IEEE-754 bits of 10.0f (older builds l32r this from a literal)
ENTRY_OPCODE      = 0x36        # first byte of the Xtensa `entry` (function prologue)
FLOOR_FIELD       = 8           # floor byte lives at  PID-object + 8
CEIL_FIELD        = 9           # ceiling byte lives at PID-object + 9


# ---- ESP32 image container -------------------------------------------------
class Image(NamedTuple):
    code_va:   int     # virtual address the code (IROM) segment loads at
    code_foff: int     # file offset of that segment's bytes
    code_len:  int     # its length
    cksum_off: int     # file offset of the image's XOR-checksum byte
    img_len:   int     # total image length (segments + checksum + optional SHA-256)
    hashed:    bool    # whether a 32-byte SHA-256 is appended


# stock partition table: two app slots (ota_0 / ota_1) + the otadata "which slot boots" record
APP_SLOTS = (0x10000, 0x3e0000)
SLOT_LEN  = 0x3d0000
OTADATA   = 0xe000


def active_slot(flash: bytes) -> int:
    """Slot the bootloader starts: newest valid otadata entry of the two copies; blank = slot 0."""
    best = 0
    for off in (OTADATA, OTADATA + 0x1000):
        seq, = struct.unpack_from("<I", flash, off)
        state, crc = struct.unpack_from("<II", flash, off + 24)
        valid = seq != 0xFFFFFFFF and crc == zlib.crc32(flash[off:off + 4], 0xFFFFFFFF)
        if valid and state not in (3, 4):           # skip INVALID / ABORTED images
            best = max(best, seq)
    return (best - 1) % len(APP_SLOTS) if best else 0


def parse_image(data: bytes) -> Image:
    """Walk the ESP32 image header + segment table to find the code segment and the
    true image length (so we can repair the checksum/hash after patching)."""
    if data[0] != 0xE9:
        sys.exit("not an ESP32 image (first byte is not 0xE9)")
    segment_count = data[1]
    off = 24                                        # size of esp_image_header_t
    segments = []                                   # (load_addr, file_off, length)
    for _ in range(segment_count):
        load_addr, length = struct.unpack_from("<II", data, off)
        off += 8
        segments.append((load_addr, off, length))
        off += length
    checksum_off = off + (15 - (off % 16))          # checksum byte ends the 16-byte-aligned tail
    img_len = checksum_off + 1
    hashed = data[23] == 1                           # esp_image_header_t.hash_appended
    if hashed:
        img_len += 32
    code = [s for s in segments if (s[0] >> 24) == 0x42]   # IROM maps at 0x42xx_xxxx
    if len(code) != 1:
        sys.exit(f"expected exactly one IROM (0x42xxxxxx) segment, found {len(code)}")
    load_addr, file_off, length = code[0]
    return Image(load_addr, file_off, length, checksum_off, img_len, hashed)


# ---- Xtensa instruction decoders (return None if the bytes aren't that op) --
def instr_len(code: bytes, i: int) -> int:
    return 2 if (code[i] & 0xf) >= 8 else 3

def decode_s8i(code, i):          # s8i aT, aS, imm8  ->  (t, s, imm);   e.g. `32 42 08` = s8i a3,a2,8
    if (code[i] & 0xf) == 2 and (code[i+1] >> 4) == 4:
        return code[i] >> 4, code[i+1] & 0xf, code[i+2]

def decode_movi(code, i):         # movi aT, imm12 (3 bytes)  ->  (t, imm);  `b2 a0 0a` = movi a11,10
    if (code[i] & 0xf) == 2 and (code[i+1] >> 4) == 0xa:
        imm = ((code[i+1] & 0xf) << 8) | code[i+2]
        return code[i] >> 4, imm - 0x1000 if imm & 0x800 else imm

def decode_movi_n(code, i):       # movi.n aT, imm7 (2 bytes)  ->  (t, imm);  `0c ab` = movi.n a11,10
    if (code[i] & 0xf) == 0xc:
        v = code[i] | (code[i+1] << 8)
        imm = ((v >> 12) & 0xf) | (((v >> 4) & 0x7) << 4)
        return (v >> 8) & 0xf, imm - 0x80 if imm & 0x40 else imm

def decode_any_movi(code, i):     # either movi form  ->  (reg, imm, width_in_bytes)
    m = decode_movi(code, i)
    if m:
        return m[0], m[1], 3
    m = decode_movi_n(code, i)
    if m:
        return m[0], m[1], 2

def decode_call_target(code, i, va):   # call0/4/8/12 at virtual address `va` -> callee va
    if (code[i] & 0xf) != 5:            # (the high bits of byte0 are offset bits, not opcode)
        return None
    instr = code[i] | (code[i+1] << 8) | (code[i+2] << 16)
    off = instr >> 6
    if off & 0x20000:
        off -= 0x40000
    return (va & ~3) + (off << 2) + 4

def decode_lsi(code, i):          # lsi fT, aS, imm (float load; imm is x4)  ->  (ft, s, byte_offset)
    if (code[i] & 0xf) == 3 and (code[i+1] >> 4) == 0:
        return code[i] >> 4, code[i+1] & 0xf, code[i+2] * 4

def is_slli(code, i):             # slli (left-shift by constant); loose recognizer
    return (code[i] & 0xf) == 0 and ((code[i+2] >> 4) & 0xf) in (0x0, 0x1)

def decode_wfr(code, i):          # wfr fT, aS  ->  (ft, s)
    v = code[i] | (code[i+1] << 8) | (code[i+2] << 16)
    if (v & 0xf) == 0 and (v >> 20) & 0xf == 0xf and (v >> 16) & 0xf == 0xa and (v >> 4) & 0xf == 5:
        return (v >> 12) & 0xf, (v >> 8) & 0xf

def decode_ufloat_s(code, i):     # ufloat.s fT, aS, imm  ->  (ft, as, imm)
    v = code[i] | (code[i+1] << 8) | (code[i+2] << 16)
    if (v & 0xf) == 0 and (v >> 20) & 0xf == 0xd and (v >> 16) & 0xf == 0xa:
        return (v >> 12) & 0xf, (v >> 8) & 0xf, (v >> 4) & 0xf

def decode_olt(code, i):          # olt.s b, fS, fT  ->  (fs, ft)
    v = code[i] | (code[i+1] << 8) | (code[i+2] << 16)
    if (v & 0xf) == 0 and (v >> 20) & 0xf == 4 and (v >> 16) & 0xf == 0xb:
        return (v >> 8) & 0xf, (v >> 4) & 0xf

def decode_l32r(code, i, va):     # l32r aT, lit  ->  (t, literal_va);  `21 9e 5e` = l32r a2,<lit>
    if (code[i] & 0xf) != 1:
        return
    imm16 = code[i+1] | (code[i+2] << 8)
    word_off = (0xFFFF0000 | imm16) - (1 << 32)        # always a negative word offset
    return code[i] >> 4, ((va + 3) & ~3) + (word_off << 2)

def read_u32(code, base_va, va):  # read a 32-bit literal out of the code segment
    idx = va - base_va
    if 0 <= idx <= len(code) - 4:
        return int.from_bytes(code[idx:idx+4], "little")


# ---- instruction encoders (for the bytes we write) -------------------------
def enc_movi_n_a11(n):            # movi.n a11, n
    v = ((n & 0xf) << 12) | (0xb << 8) | (((n >> 4) & 0x7) << 4) | 0xc
    return bytes([v & 0xff, v >> 8])

def enc_movi_a11(n):              # movi a11, n
    return bytes([0xb2, 0xa0, n & 0xff])

def enc_l8ui(dst_reg, base_reg, offset):      # l8ui dst, base, offset
    return bytes([(dst_reg << 4) | 2, base_reg & 0xf, offset & 0xff])

def enc_ufloat_s(dst_freg, src_reg):          # ufloat.s dst_freg, src_reg, 0  (ft@[15:12], as@[11:8], imm@[7:4]=0)
    v = 0xda0000 | (dst_freg << 12) | (src_reg << 8)
    return bytes([v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff])

NOP = bytes([0xf0, 0x20, 0x00])


# ---- located sites ---------------------------------------------------------
class FloorSite(NamedTuple):
    off:   int     # code-relative offset of the `movi a11, <floor>`
    imm:   int     # the floor value currently there (10 in stock)
    width: int     # 2 (movi.n) or 3 (movi)

class Setter(NamedTuple):
    entry:       int             # virtual address of the bounds-setter function
    floor_sites: List[FloorSite] # the floor `movi`s in its callers (what we patch)
    pidoff:      int             # offset of the PID sub-object inside the controller

class Threshold(NamedTuple):
    setpoint_off: int    # field offset of the setpoint it compares 10.0 against
    olt_va:       int    # virtual address of the compare (for logging)
    variant:      str    # "inline" (movi+slli+wfr) or "l32r" (literal load)
    edits:        list   # [(code_off, new_bytes), ...] rewriting the 10.0 into a floor-byte read


# ---- step 1: find the floor (bounds-setter + its callers) ------------------
def nearest_movi_before(code, call_off, reg, window):
    """The last `movi(.n) regN, k` in [call_off-window, call_off). Returns (off, imm, width)."""
    found = None
    for k in range(max(0, call_off - window), call_off):
        m = decode_any_movi(code, k)
        if m and m[0] == reg:
            found = (k, m[1], m[2])
    return found

def setter_candidates(code, base_va):
    """Every tiny function whose body stores arg2->+8 and arg3->+9 in the ABI shape
    (base object in a2, low in a3, high in a4). Returns their entry virtual addresses."""
    entries = []
    for i in range(len(code) - 6):
        lo = decode_s8i(code, i)
        hi = decode_s8i(code, i + 3)
        shape = (lo and hi and lo[2] == FLOOR_FIELD and hi[2] == CEIL_FIELD
                 and lo[1] == hi[1] == 2 and lo[0] == 3 and hi[0] == 4)
        if not shape:
            continue
        for j in range(i, max(0, i - ENTRY_SCAN_BACK), -1):   # back up to the `entry` prologue
            if code[j] == ENTRY_OPCODE:
                entries.append(base_va + j)
                break
    return entries

def inspect_callers(code, base_va, setter_entry):
    """Look at everyone who calls `setter_entry`. If they look like the PID bounds-setter
    (each passes a constant floor in a11, and a consistent PID offset in a10) return
    (floor_sites, pidoff); otherwise None."""
    floor_sites, pidoffs = [], set()
    for i in range(len(code) - 2):
        if (code[i] & 0xf) != 5:
            continue
        if decode_call_target(code, i, base_va + i) != setter_entry:
            continue
        floor = nearest_movi_before(code, i, 11, FLOOR_SCAN_BACK)      # a11 = 2nd arg = floor
        if not (floor and CALLER_FLOOR_RANGE[0] <= floor[1] <= CALLER_FLOOR_RANGE[1]):
            continue                                                  # not a "pass a constant floor" call
        floor_sites.append(FloorSite(floor[0], floor[1], floor[2]))
        base = nearest_movi_before(code, i, 10, PIDOFF_SCAN_BACK)      # a10 = 1st arg = controller+PIDOFF
        if base and PIDOFF_RANGE[0] <= base[1] <= PIDOFF_RANGE[1]:
            pidoffs.add(base[1])
    if floor_sites and len(pidoffs) == 1:
        return floor_sites, pidoffs.pop()
    return None

def find_floor(code, base_va, log) -> Setter:
    matches = []
    for entry in setter_candidates(code, base_va):
        found = inspect_callers(code, base_va, entry)
        log(f"candidate setter @ {entry:#x}: "
            + (f"{len(found[0])} floor site(s)={[s.imm for s in found[0]]}, PIDOFF={found[1]:#x}"
               if found else "no valid floor callers"))
        if found:
            matches.append(Setter(entry, found[0], found[1]))
    if len(matches) != 1:
        sys.exit(f"bounds-setter not uniquely identified ({len(matches)} candidates) — refusing.")
    s = matches[0]
    log(f"floor: setter @ {s.entry:#x}, PIDOFF={s.pidoff:#x} -> floor byte at object+{s.pidoff + FLOOR_FIELD:#x}")
    return s


# ---- step 2: find the off-threshold (10.0-vs-setpoint compare) -------------
def _compare_to_setpoint(code, base_va, const_start, wfr_off, float_reg):
    """After the constant 10.0 lands in `float_reg` (via the `wfr` at wfr_off), find the
    `olt.s` comparing it to a setpoint and the `lsi` that loaded that setpoint.
    Returns (setpoint_off, base_reg, olt_va) or None."""
    other = olt_off = None
    k = wfr_off
    for _ in range(8):
        k += instr_len(code, k)
        if k >= len(code) - 2:
            break
        o = decode_olt(code, k)
        if o and float_reg in o:
            other = o[1] if o[0] == float_reg else o[0]
            olt_off = k
            break
    if other is None:
        return None
    base_reg = setpoint_off = None
    for k in range(max(0, const_start - 0x18), olt_off):
        ld = decode_lsi(code, k)
        if ld and ld[0] == other:
            base_reg, setpoint_off = ld[1], ld[2]
    if base_reg is None:
        return None
    return setpoint_off, base_reg, base_va + olt_off

def _find_wfr(code, start, src_reg, span=5):
    """First `wfr fD, src_reg` at/after `start`. Returns (wfr_off, fD) or None."""
    k = start
    for _ in range(span):
        if k >= len(code) - 2:
            break
        w = decode_wfr(code, k)
        if w and w[1] == src_reg:
            return k, w[0]
        k += instr_len(code, k)

def threshold_candidates(code, base_va, floor_byte):
    """Find every "10.0 < setpoint" check — in EITHER compiler form — and precompute the
    edits that rewrite its constant 10.0 into a read of the floor byte:
      inline  (newer): movi aS,0x209 ; slli aS,aS,21 ; wfr fD,aS
      literal (older): l32r aX,<10.0f literal>        ; wfr fD,aX
    Both rewrite to:   l8ui <reg>,<setpoint base>,floor_byte ; ufloat.s fD,<reg>   (inline also nops the slli slot)."""
    found = []
    for i in range(len(code) - 3):
        # inline form: movi aS, 0x209  (then slli + wfr finish 10.0f)
        built = decode_movi(code, i)
        if built and built[1] == C10_MOVI_IMM:
            scratch = built[0]
            shift_off, k = None, i + 3
            for _ in range(5):
                if k >= len(code) - 2:
                    break
                if is_slli(code, k) and (code[k+1] & 0xf) == scratch:
                    shift_off = k
                    break
                k += instr_len(code, k)
            wfr = _find_wfr(code, i + 3, scratch)
            if shift_off is not None and wfr:
                cmp = _compare_to_setpoint(code, base_va, i, wfr[0], wfr[1])
                if cmp:
                    sp_off, base_reg, olt_va = cmp
                    edits = [(i,         enc_l8ui(scratch, base_reg, floor_byte)),
                             (shift_off, enc_ufloat_s(wfr[1], scratch)),
                             (wfr[0],    NOP)]
                    found.append(Threshold(sp_off, olt_va, "inline", edits))
            continue
        # literal form: l32r aX, <literal that holds 10.0f>  (then wfr)
        lr = decode_l32r(code, i, base_va + i)
        if lr and read_u32(code, base_va, lr[1]) == C10_FLOAT:
            scratch = lr[0]
            wfr = _find_wfr(code, i + 3, scratch)
            if wfr:
                cmp = _compare_to_setpoint(code, base_va, i, wfr[0], wfr[1])
                if cmp:
                    sp_off, base_reg, olt_va = cmp
                    edits = [(i,      enc_l8ui(scratch, base_reg, floor_byte)),
                             (wfr[0], enc_ufloat_s(wfr[1], scratch))]
                    found.append(Threshold(sp_off, olt_va, "l32r", edits))
    return found

def find_threshold(code, base_va, pidoff, log) -> Threshold:
    # The CH-enable threshold compares the SETPOINT, which lives just below the PID
    # sub-object. Of all 10.0-vs-field compares, pick the one whose field is the
    # largest offset still below the PID object; require it unique.
    cands = threshold_candidates(code, base_va, pidoff + FLOOR_FIELD)
    below = [t for t in cands if t.setpoint_off < pidoff]
    if not below:
        sys.exit(f"off-threshold: found no 10.0-vs-setpoint compare below the PID object "
                 f"({len(cands)} candidate(s)) — refusing (would short-cycle). Patch it manually.")
    top = max(t.setpoint_off for t in below)
    winners = [t for t in below if t.setpoint_off == top]
    if len(winners) != 1:
        sys.exit(f"off-threshold ambiguous ({len(winners)} compares at object+{top:#x}) — refusing.")
    t = winners[0]
    log(f"threshold: {t.variant} form, 10.0 vs setpoint(object+{t.setpoint_off:#x}) @ {t.olt_va:#x}")
    return t


# ---- step 3: apply, repair integrity, verify -------------------------------
def patch_image(data: bytearray, img: Image, setter: Setter, thr: Threshold, floor: int):
    """Write the floor + threshold edits, then repair the XOR checksum and SHA-256."""
    code = data[img.code_foff:img.code_foff + img.code_len]
    delta = 0                                           # running XOR of every byte we change

    def write(off, new):
        nonlocal delta
        for k in range(len(new)):
            delta ^= code[off + k] ^ new[k]
        code[off:off + len(new)] = new

    for site in setter.floor_sites:                     # movi a11, 10  ->  movi a11, <floor>
        write(site.off, enc_movi_n_a11(floor) if site.width == 2 else enc_movi_a11(floor))
    for off, new_bytes in thr.edits:                    # rewrite the 10.0 constant into a floor-byte read
        write(off, new_bytes)

    data[img.code_foff:img.code_foff + img.code_len] = code
    data[img.cksum_off] ^= delta
    if img.hashed:
        data[img.img_len - 32:img.img_len] = hashlib.sha256(bytes(data[:img.img_len - 32])).digest()
    return bytes(data[:img.img_len])

def verify(out: bytes, img: Image, setter: Setter, thr: Threshold, floor: int) -> bool:
    """Re-decode the patched sites in the output to confirm they say what we intended."""
    code = out[img.code_foff:img.code_foff + img.code_len]
    floors_ok = all((m := decode_any_movi(code, s.off)) and m[0] == 11 and m[1] == floor
                    for s in setter.floor_sites)
    floor_byte = setter.pidoff + FLOOR_FIELD
    b = code[thr.edits[0][0]:thr.edits[0][0] + 3]        # edit 0: should be l8ui <reg>, <base>, floor_byte
    l8_ok = (b[0] & 0xf) == 2 and (b[1] >> 4) == 0 and b[2] == floor_byte
    reg = b[0] >> 4
    uf = decode_ufloat_s(code, thr.edits[1][0])          # edit 1: should be ufloat.s <fD>, <reg>, 0
    uf_ok = bool(uf) and uf[1] == reg and uf[2] == 0
    return floors_ok and l8_ok and uf_ok


def main():
    ap = argparse.ArgumentParser(description="Locate-and-patch the Thermostat 3 PID floor.")
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--floor", type=int, default=35, help="new floor / min flow temp (default 35)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if not (FLOOR_ARG_RANGE[0] <= args.floor <= FLOOR_ARG_RANGE[1]):
        sys.exit(f"--floor must be {FLOOR_ARG_RANGE[0]}..{FLOOR_ARG_RANGE[1]}")
    log = print if args.verbose else (lambda *a: None)

    data = bytearray(open(args.src, "rb").read())
    if len(data) > SLOT_LEN:                        # full flash backup, not a single app image
        slot = active_slot(data)
        print(f"full backup: thermostat boots slot {slot} ({APP_SLOTS[slot]:#x}), patching that one")
        data = data[APP_SLOTS[slot]:APP_SLOTS[slot] + SLOT_LEN]
    if struct.unpack_from("<I", data, 0x20)[0] == 0xABCD5432:   # esp_app_desc_t
        tag, date = (bytes(data[o:o + 32]).split(b"\0")[0].decode(errors="replace") for o in (0x30, 0x80))
        print(f"firmware build: {tag} ({date})")
    img = parse_image(data)
    code = bytes(data[img.code_foff:img.code_foff + img.code_len])
    log(f"IROM: va {img.code_va:#x}, file {img.code_foff:#x}, len {img.code_len:#x}; "
        f"image_len {img.img_len:#x} hash={img.hashed}")

    setter = find_floor(code, img.code_va, log)
    thr = find_threshold(code, img.code_va, setter.pidoff, log)

    out = patch_image(data, img, setter, thr, args.floor)
    open(args.dst, "wb").write(out)

    ok = verify(out, img, setter, thr, args.floor)
    print(f"{'OK' if ok else 'FAIL'}: wrote {args.dst}  floor={args.floor} at {len(setter.floor_sites)} "
          f"site(s); off-threshold now reads object+{setter.pidoff + FLOOR_FIELD:#x}; checksum+SHA fixed")
    if not ok:
        sys.exit("post-patch verification FAILED — do not flash")


if __name__ == "__main__":
    main()
