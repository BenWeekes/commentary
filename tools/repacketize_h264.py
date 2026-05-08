#!/usr/bin/env python3

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


START3 = b"\x00\x00\x01"
START4 = b"\x00\x00\x00\x01"


@dataclass
class NAL:
    data: bytes
    nal_type: int


def find_start_code(data: bytes, start: int) -> tuple[int, int] | None:
    i = start
    end = len(data) - 3
    while i < end:
        if data[i : i + 3] == START3:
            return i, 3
        if i + 4 <= len(data) and data[i : i + 4] == START4:
            return i, 4
        i += 1
    return None


def split_nals(data: bytes) -> list[NAL]:
    out: list[NAL] = []
    pos = 0
    while True:
        found = find_start_code(data, pos)
        if not found:
            break
        start, sc_len = found
        hdr = start + sc_len
        next_found = find_start_code(data, hdr)
        end = next_found[0] if next_found else len(data)
        if hdr < len(data):
            nal = data[start:end]
            out.append(NAL(data=nal, nal_type=data[hdr] & 0x1F))
        pos = end
    return out


def group_by_aud(nals: list[NAL]) -> list[list[NAL]]:
    groups: list[list[NAL]] = []
    current: list[NAL] = []
    for nal in nals:
        if nal.nal_type == 9 and current:
            groups.append(current)
            current = []
        current.append(nal)
    if current:
        groups.append(current)
    return groups


def is_key_group(group: list[NAL]) -> bool:
    return any(n.nal_type == 5 for n in group)


def write_repacketized(groups: list[list[NAL]]) -> bytes:
    sps: list[bytes] = []
    pps: list[bytes] = []
    out = bytearray()
    first_written = False

    for group in groups:
        # Track latest SPS/PPS seen in the source.
        for nal in group:
            if nal.nal_type == 7:
                sps = [nal.data]
            elif nal.nal_type == 8:
                pps = [nal.data]

        key = is_key_group(group)
        if key:
            if sps:
                out.extend(sps[0])
            if pps:
                out.extend(pps[0])
            # Keep a single SEI on the first key AU only. The demo stream is much cleaner.
            if not first_written:
                for nal in group:
                    if nal.nal_type == 6:
                        out.extend(nal.data)
                        break
            for nal in group:
                if nal.nal_type == 5:
                    out.extend(nal.data)
            first_written = True
            continue

        # Inter frame AU: keep only slice NALs.
        for nal in group:
            if nal.nal_type == 1:
                out.extend(nal.data)

    return bytes(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean Annex B H264 from SRT-style AUD/filler-heavy bytestreams.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    data = args.input.read_bytes()
    groups = group_by_aud(split_nals(data))
    out = write_repacketized(groups)
    args.output.write_bytes(out)
    print(f"wrote {args.output} ({len(out)} bytes) from {len(groups)} AUs")


if __name__ == "__main__":
    main()
