"""Deterministic binary session analysis engine.

The engine is a pure function of (frames, protocol document).  Given the
same inputs it always produces the same event stream with stable event
identifiers (``e0000``, ``e0001``, ...), which is what makes branch
re-execution and offline replay verification possible.

Design rules:

* Recoverable problems (bad checksum, resync, gap, reorder, duplicate,
  time rollback, truncated UTF-8) produce ``warning`` events and flag the
  session as ``recovering``.
* Protocol violations that leave the state machine undefined (unknown
  message type, unexpected message for the current state) are ``fatal``
  and reject the session.
* The engine never invents bytes: gaps are reported and missing sequence
  numbers are listed, but no synthetic segment is ever processed.
"""
from __future__ import annotations

import hashlib
import json

HEADER_SIZE = 8
CRC_SIZE = 2
MIN_SEGMENT = HEADER_SIZE + CRC_SIZE

SEVERITY_ORDER = {"fatal": 0, "error": 1, "warning": 2, "info": 3}


def crc16_ccitt(data, init=0xFFFF):
    crc = init & 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def protocol_key(doc):
    return "%s@%s" % (doc["name"], doc["version"])


def base_protocol(version=1):
    """Built-in versioned protocol description for the handshake format."""
    return {
        "name": "handshake",
        "version": version,
        "magic": "fb",
        "version_byte": version,
        "crc_init": "0xffff" if version == 1 else "0x1d0f",
        "msg_types": {
            "0": "HELLO",
            "1": "CHALLENGE",
            "2": "RESPONSE",
            "3": "ACCEPT",
            "4": "DATA",
            "5": "FIN",
        },
        "transitions": {
            "INIT": {"HELLO": "HELLO_SENT"},
            "HELLO_SENT": {"CHALLENGE": "CHALLENGED"},
            "CHALLENGED": {"RESPONSE": "RESPONDED"},
            "RESPONDED": {"ACCEPT": "ESTABLISHED"},
            "ESTABLISHED": {"DATA": "ESTABLISHED", "FIN": "CLOSED"},
            "CLOSED": {},
        },
        "accept_states": ["ESTABLISHED", "CLOSED"],
        "utf8_fields": {"HELLO": "client_name"},
        "reorder_window": 4,
    }


def event_signature(event):
    return [
        event["id"],
        event["kind"],
        event["severity"],
        event["message"],
        event["state_from"],
        event["state_to"],
        event["frame_id"],
        event["byte_start"],
        event["byte_end"],
    ]


def digest_events(events):
    payload = json.dumps(
        [event_signature(e) for e in events],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compress_seqs(seqs):
    seqs = sorted(seqs)
    if not seqs:
        return ""
    ranges = []
    start = prev = seqs[0]
    for value in seqs[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append("%d" % start if start == prev else "%d-%d" % (start, prev))
        start = prev = value
    ranges.append("%d" % start if start == prev else "%d-%d" % (start, prev))
    return ",".join(ranges)


def analyze(frames, protocol):
    """Analyze a capture. ``frames`` is a list of dicts with keys
    frame_id/session/ts/direction/data(bytes)/arrival(int)."""
    magic = int(str(protocol.get("magic", "fb")), 16)
    version_byte = int(protocol.get("version_byte", 1))
    crc_init = int(str(protocol.get("crc_init", "0xffff")), 16)
    msg_types = {int(k): v for k, v in protocol["msg_types"].items()}
    transitions = protocol["transitions"]
    accept_states = set(protocol.get("accept_states", []))
    utf8_fields = protocol.get("utf8_fields", {})
    window = int(protocol.get("reorder_window", 4))

    frames = sorted(frames, key=lambda f: f["arrival"])
    frames_by_id = {f["frame_id"]: f for f in frames}

    events = []
    sessions = {}
    seen_frames = {}

    def emit(session, kind, severity, message, frame_id=None, byte_start=None,
             byte_end=None, ts=None, state_from=None, state_to=None, extra=None):
        event = {
            "id": "e%04d" % len(events),
            "idx": len(events),
            "session": session,
            "kind": kind,
            "severity": severity,
            "message": message,
            "frame_id": frame_id,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "ts": ts,
            "state_from": state_from,
            "state_to": state_to,
        }
        if extra:
            event.update(extra)
        events.append(event)
        return event

    def get_session(name):
        if name not in sessions:
            sessions[name] = {
                "name": name,
                "state": "INIT",
                "status": "pending",
                "warnings": 0,
                "dead": False,
                "last_ts": None,
                "channels": {},
            }
        return sessions[name]

    def get_channel(sess, direction):
        channels = sess["channels"]
        if direction not in channels:
            channels[direction] = {
                "buf": bytearray(),
                "prov": [],
                "expected": 0,
                "pending": {},
            }
        return channels[direction]

    def warn(sess, kind, message, **kw):
        sess["warnings"] += 1
        return emit(sess["name"], kind, "warning", message, **kw)

    def frame_range(prov, start, end):
        """Byte range of segment bytes [start, end) mapped onto one frame."""
        fid0, off0 = prov[start]
        fid1, off1 = prov[end - 1]
        if fid0 != fid1:
            return fid0, off0, None
        return fid0, off0, off1 + 1

    def drop(ch, count):
        del ch["buf"][:count]
        del ch["prov"][:count]

    def parse_channel(sess, ch):
        while True:
            buf = ch["buf"]
            if len(buf) < MIN_SEGMENT:
                return
            fid0, off0 = ch["prov"][0]
            ts0 = frames_by_id[fid0]["ts"]
            if buf[0] != magic:
                warn(sess, "resync",
                     "expected magic 0x%02x, found 0x%02x; dropping 1 byte"
                     % (magic, buf[0]),
                     frame_id=fid0, byte_start=off0, byte_end=off0 + 1, ts=ts0)
                drop(ch, 1)
                continue
            if buf[1] != version_byte:
                warn(sess, "unsupported_version",
                     "unsupported protocol version byte %d; dropping 1 byte"
                     % buf[1],
                     frame_id=fid0, byte_start=off0, byte_end=off0 + 1, ts=ts0)
                drop(ch, 1)
                continue
            plen = (buf[6] << 8) | buf[7]
            total = HEADER_SIZE + plen + CRC_SIZE
            if len(buf) < total:
                return
            seg = bytes(buf[:total])
            prov = list(ch["prov"][:total])
            drop(ch, total)
            crc_actual = (seg[-2] << 8) | seg[-1]
            crc_want = crc16_ccitt(seg[:-2], crc_init)
            fid, bstart, bend = frame_range(prov, 0, total)
            if crc_actual != crc_want:
                warn(sess, "checksum",
                     "crc mismatch: got 0x%04x, computed 0x%04x; segment dropped"
                     % (crc_actual, crc_want),
                     frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0)
                continue
            handle_segment(sess, ch, seg, prov)

    def handle_segment(sess, ch, seg, prov):
        seq = (seg[4] << 8) | seg[5]
        expected = ch["expected"]
        fid, bstart, bend = frame_range(prov, 0, len(seg))
        ts0 = frames_by_id[prov[0][0]]["ts"]
        if seq < expected:
            warn(sess, "retransmit",
                 "segment seq %d already consumed; ignored" % seq,
                 frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0)
            return
        if seq > expected:
            if seq in ch["pending"]:
                warn(sess, "duplicate_segment",
                     "segment seq %d already buffered; ignored" % seq,
                     frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0)
                return
            if seq - expected > window:
                missing = list(range(expected, seq))
                warn(sess, "gap",
                     "missing seq %s; continuing at seq %d without "
                     "fabricating bytes" % (_compress_seqs(missing), seq),
                     frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0)
                for held in sorted(ch["pending"]):
                    if held < seq:
                        ch["pending"].pop(held)
                        warn(sess, "gap_discard",
                             "buffered segment seq %d discarded after gap" % held)
                ch["expected"] = seq
            else:
                ch["pending"][seq] = (seg, prov)
                warn(sess, "out_of_order",
                     "segment seq %d arrived before seq %d; buffered"
                     % (seq, expected),
                     frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0)
                return
        process_segment(sess, ch, seg, prov)
        ch["expected"] += 1
        while ch["expected"] in ch["pending"]:
            seg2, prov2 = ch["pending"].pop(ch["expected"])
            process_segment(sess, ch, seg2, prov2)
            ch["expected"] += 1

    def process_segment(sess, ch, seg, prov):
        fid, bstart, bend = frame_range(prov, 0, len(seg))
        ts0 = frames_by_id[prov[0][0]]["ts"]
        msg = seg[2]
        name = msg_types.get(msg)
        payload = seg[HEADER_SIZE:len(seg) - CRC_SIZE]
        if name is None:
            sess["dead"] = True
            sess["status"] = "rejected"
            emit(sess["name"], "unknown_message", "fatal",
                 "unknown message type %d; session rejected" % msg,
                 frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0)
            return
        emit(sess["name"], "message", "info",
             "%s seq=%d payload=%d bytes" % (name, (seg[4] << 8) | seg[5],
                                             len(payload)),
             frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0,
             extra={"msg_type": name})
        if name in utf8_fields and payload:
            field = utf8_fields[name]
            pfid, pstart, pend = frame_range(
                prov, HEADER_SIZE, HEADER_SIZE + len(payload))
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                warn(sess, "utf8",
                     "field %s is not valid UTF-8 (%s); bytes preserved, "
                     "nothing padded" % (field, exc),
                     frame_id=pfid, byte_start=pstart, byte_end=pend, ts=ts0)
        if sess["dead"]:
            emit(sess["name"], "ignored", "info",
                 "session already rejected; segment ignored",
                 frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0)
            return
        state = sess["state"]
        nxt = transitions.get(state, {}).get(name)
        if nxt is None:
            sess["dead"] = True
            sess["status"] = "rejected"
            emit(sess["name"], "unexpected_message", "fatal",
                 "%s not allowed in state %s; session rejected" % (name, state),
                 frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0,
                 state_from=state, state_to=None)
            return
        emit(sess["name"], "state_transition", "info",
             "%s --%s--> %s" % (state, name, nxt),
             frame_id=fid, byte_start=bstart, byte_end=bend, ts=ts0,
             state_from=state, state_to=nxt)
        sess["state"] = nxt
        if nxt in accept_states:
            sess["status"] = "accepted"

    for frame in frames:
        sess = get_session(frame["session"])
        data = frame["data"]
        if sess["last_ts"] is not None and frame["ts"] < sess["last_ts"]:
            warn(sess, "time_rollback",
                 "timestamp %.6f predates previous %.6f; kept as-is"
                 % (frame["ts"], sess["last_ts"]),
                 frame_id=frame["frame_id"], ts=frame["ts"])
        sess["last_ts"] = frame["ts"]
        digest = hashlib.sha1(data).hexdigest()
        key = (frame["session"], frame["direction"], digest)
        if key in seen_frames:
            warn(sess, "duplicate_frame",
                 "duplicate of frame %s; ignored" % seen_frames[key],
                 frame_id=frame["frame_id"], byte_start=0, byte_end=len(data),
                 ts=frame["ts"])
            continue
        seen_frames[key] = frame["frame_id"]
        emit(sess["name"], "frame", "info",
             "frame %s: %d bytes %s"
             % (frame["frame_id"], len(data), frame["direction"]),
             frame_id=frame["frame_id"], byte_start=0, byte_end=len(data),
             ts=frame["ts"])
        ch = get_channel(sess, frame["direction"])
        ch["buf"].extend(data)
        ch["prov"].extend((frame["frame_id"], i) for i in range(len(data)))
        parse_channel(sess, ch)

    for sess in sessions.values():
        for direction, ch in sorted(sess["channels"].items()):
            if ch["pending"]:
                warn(sess, "unflushed_gap",
                     "session ended with unrecovered gap; buffered seq %s "
                     "never processed"
                     % _compress_seqs(sorted(ch["pending"])))
        if sess["status"] == "pending" and sess["warnings"]:
            sess["status"] = "recovering"

    summary = {
        name: {
            "state": s["state"],
            "status": s["status"],
            "warnings": s["warnings"],
            "rejected": s["dead"],
        }
        for name, s in sessions.items()
    }
    return {
        "events": events,
        "sessions": summary,
        "digest": digest_events(events),
    }


def first_divergence(base_events, branch_events):
    """First index where two event streams diverge, or None if identical."""
    total = max(len(base_events), len(branch_events))
    for idx in range(total):
        sig_a = event_signature(base_events[idx]) if idx < len(base_events) else None
        sig_b = event_signature(branch_events[idx]) if idx < len(branch_events) else None
        if sig_a != sig_b:
            return {
                "index": idx,
                "event_id": "e%04d" % idx,
                "base": base_events[idx] if idx < len(base_events) else None,
                "branch": branch_events[idx] if idx < len(branch_events) else None,
            }
    return None
