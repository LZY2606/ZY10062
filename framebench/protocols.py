"""Versioned protocol descriptions.

A protocol description is a versioned, self-contained spec the analyzer uses
to parse frames, verify checksums and drive the handshake state machine.
Two built-in versions ship with the package; captures and branches always
bind to an explicit version so upgrades never silently change old results.
"""

from __future__ import annotations

import copy
import json

# Message type codes shared by both protocol versions.
MSG_TYPES = {
    0x01: "HELLO",
    0x02: "HELLO_ACK",
    0x03: "DATA",
    0x04: "FIN",
    0x05: "FIN_ACK",
    0x06: "RESET",
    0x07: "PING",
    0x08: "PONG",
}

FLAG_FRAG = 0x01  # more fragments follow for this message
FLAG_URGENT = 0x02

_PROTOCOL_V1 = {
    "name": "nbp",
    "version": "1.0",
    "magic": "4e42",
    "header_len": 12,
    "fields": {
        # name: [offset, length_bytes]
        "magic": [0, 2],
        "version": [2, 1],
        "session": [3, 2],
        "seq": [5, 4],
        "flags": [9, 1],
        "plen": [10, 2],
    },
    "checksum": {"algo": "crc32", "offset_from_end": 4, "covers": "header+payload"},
    "msg_types": {k: v for k, v in MSG_TYPES.items() if k <= 0x06},
    "text_msg_types": ["DATA"],
    "states": ["INIT", "SYN_SENT", "ESTABLISHED", "FIN_WAIT", "CLOSED"],
    "transitions": [
        {"from": "INIT", "msg": "HELLO", "dir": "c2s", "to": "SYN_SENT"},
        {"from": "SYN_SENT", "msg": "HELLO_ACK", "dir": "s2c", "to": "ESTABLISHED"},
        {"from": "ESTABLISHED", "msg": "DATA", "dir": "*", "to": "ESTABLISHED"},
        {"from": "ESTABLISHED", "msg": "FIN", "dir": "*", "to": "FIN_WAIT"},
        {"from": "FIN_WAIT", "msg": "FIN_ACK", "dir": "*", "to": "CLOSED"},
        {"from": "*", "msg": "RESET", "dir": "*", "to": "CLOSED"},
    ],
}

_PROTOCOL_V2 = copy.deepcopy(_PROTOCOL_V1)
_PROTOCOL_V2.update(
    {
        "version": "2.0",
        "header_len": 14,
        "msg_types": dict(MSG_TYPES),
    }
)
_PROTOCOL_V2["fields"] = {
    "magic": [0, 2],
    "version": [2, 1],
    "session": [3, 2],
    "channel": [5, 2],
    "seq": [7, 4],
    "flags": [11, 1],
    "plen": [12, 2],
}
_PROTOCOL_V2["transitions"] = _PROTOCOL_V2["transitions"] + [
    {"from": "ESTABLISHED", "msg": "PING", "dir": "*", "to": "ESTABLISHED"},
    {"from": "ESTABLISHED", "msg": "PONG", "dir": "*", "to": "ESTABLISHED"},
]

_REGISTRY = {"1.0": _PROTOCOL_V1, "2.0": _PROTOCOL_V2}


def list_protocols() -> list[dict]:
    return [
        {"name": spec["name"], "version": spec["version"], "header_len": spec["header_len"]}
        for spec in _REGISTRY.values()
    ]


def get_protocol(version: str) -> dict:
    try:
        return copy.deepcopy(_REGISTRY[version])
    except KeyError:
        raise KeyError(f"unknown protocol version: {version}") from None


def default_version() -> str:
    return "1.0"


def spec_json(version: str) -> str:
    return json.dumps(get_protocol(version), sort_keys=True)


def check_migration(from_version: str, to_version: str) -> list[str]:
    """Static compatibility check between two protocol versions.

    Returns a list of human-readable findings; an empty list means the
    migration is statically safe. Frame-level checks happen at run time.
    """
    src, dst = get_protocol(from_version), get_protocol(to_version)
    findings: list[str] = []
    if src["magic"] != dst["magic"]:
        findings.append(f"magic changed {src['magic']} -> {dst['magic']}")
    if src["header_len"] != dst["header_len"]:
        findings.append(
            f"header length changed {src['header_len']} -> {dst['header_len']}; "
            "frames will be re-parsed against the new layout"
        )
    removed = set(src["msg_types"].values()) - set(dst["msg_types"].values())
    if removed:
        findings.append(f"message types removed: {sorted(removed)}")
    for tr in src["transitions"]:
        if tr not in dst["transitions"]:
            findings.append(f"transition removed: {tr['from']} --{tr['msg']}--> {tr['to']}")
    return findings
