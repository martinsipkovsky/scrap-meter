"""Piece rules: when one real piece is judged from several camera pictures.

A job can have a piece rule (Job.piece_rule). Without one (or with 1 picture
per piece) every OK / NOK the device counts is a piece, as before. With one,
the source's OK / NOK pictures are grouped into pieces before anything is
counted, so the dashboard, statistics, OEE, notifications, chat commands and
the daily data all count pieces. Readings keep the device's raw picture
counters (raw_ok / raw_nok); ok_added / nok_added are pieces.

The rule (all keys optional):

* ``pictures``: N pictures make one piece (1 = off).
* ``verdict``: "all_ok" (default): a piece is OK only when all N pictures are
  OK; "min_ok": OK when at least ``min_ok`` (K) of the N pictures are OK.
* ``nok_closes``: the first NOK picture that makes the piece NOK ends it at
  once ("next cavity when NOK"); the next picture starts a new piece.
* ``timeout_s``: a piece still missing pictures this many seconds after its
  first picture is judged anyway (when the source is read next).
* ``missing``: what a missing picture means when a piece is judged before it
  has N pictures (timeout, job change, production stop, a new piece id):
  "nok" (default: the piece is NOK), "judge" (judge only the pictures taken:
  OK unless more NOK pictures than the verdict allows) or "discard" (the
  incomplete piece isn't counted).
* ``group_key``: a device value holding a piece or cavity id. Pictures with
  the same id are one piece; a new id ends the open piece (``missing``
  applies when it has fewer than N pictures).

Order: when a source reports one picture per read (listeners, event mode) the
grouping is exact. When a polled counter rose by several pictures since the
previous read, the order of OK and NOK is unknown, so the rule assumes the
worst: every NOK picture spoils a piece of its own. Those counts are an
estimate. Pictures of an unfinished piece wait in the source's open piece
(SourceState.open_piece) for the next read.
"""
from __future__ import annotations

import time

VERDICTS = ("all_ok", "min_ok")
MISSING = ("nok", "judge", "discard")
MAX_PICTURES = 64


def normalize(raw) -> dict | None:
    """A clean rule, or None when it changes nothing (1 picture per piece and
    no piece id). Raises ValueError for an impossible rule."""
    if not raw:
        return None
    n = raw.get("pictures")
    n = 1 if n in (None, "") else int(n)
    if not 1 <= n <= MAX_PICTURES:
        raise ValueError(f"Pictures per piece must be 1-{MAX_PICTURES}")
    verdict = raw.get("verdict") or "all_ok"
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    k = n
    if verdict == "min_ok":
        k = int(raw.get("min_ok") or n)
        if not 1 <= k <= n:
            raise ValueError("At least K OK pictures: K must be between 1 and the pictures per piece")
    missing = raw.get("missing") or "nok"
    if missing not in MISSING:
        raise ValueError(f"missing must be one of {MISSING}")
    timeout = raw.get("timeout_s")
    timeout = float(timeout) if timeout not in (None, "", 0) else None
    if timeout is not None and not 0 < timeout <= 86400:
        raise ValueError("The timeout must be more than 0 and at most 86400 seconds")
    group_key = (raw.get("group_key") or "").strip() or None
    if n == 1 and not group_key:
        return None
    return {"pictures": n, "verdict": verdict, "min_ok": k, "nok_closes": bool(raw.get("nok_closes")),
            "timeout_s": timeout, "missing": missing, "group_key": group_key}


def describe(rule: dict | None) -> str:
    """The rule in words, for the UI and the docs' examples."""
    if not rule:
        return "1 picture = 1 piece"
    n = rule["pictures"]
    parts = []
    if rule.get("group_key"):
        parts.append(f"pictures with the same '{rule['group_key']}' are one piece (up to {n})")
    else:
        parts.append(f"{n} pictures = 1 piece")
    if rule["verdict"] == "min_ok" and rule["min_ok"] < n:
        parts.append(f"OK with at least {rule['min_ok']} OK pictures")
    else:
        parts.append("OK only if all pictures are OK")
    if rule.get("nok_closes"):
        parts.append("a NOK ends the piece")
    if rule.get("timeout_s"):
        parts.append(f"judged after {rule['timeout_s']:g} s")
    parts.append({"nok": "missing pictures make it NOK", "judge": "missing pictures are left out",
                  "discard": "incomplete pieces are not counted"}[rule["missing"]])
    return ", ".join(parts)


def _allowed_nok(rule: dict) -> int:
    return rule["pictures"] - rule["min_ok"]  # NOK pictures a piece may have and stay OK


def _close(rule: dict, piece: dict, complete: bool) -> tuple[int, int]:
    """(OK, NOK) pieces from closing ``piece``."""
    ok, nok = piece["ok"], piece["nok"]
    if not ok and not nok:
        return 0, 0
    if complete or nok > _allowed_nok(rule):
        return (1, 0) if nok <= _allowed_nok(rule) else (0, 1)
    if rule["missing"] == "discard":
        return 0, 0
    if rule["missing"] == "nok":
        return 0, 1
    return (1, 0) if nok <= _allowed_nok(rule) else (0, 1)  # judge the pictures taken


def _empty(now: float, pid=None) -> dict:
    return {"ok": 0, "nok": 0, "since": None, "id": pid}


def close_open(rule: dict | None, piece: dict | None) -> tuple[int, int]:
    """End an open piece before it is complete (job change, stop): the
    ``missing`` rule decides."""
    if not rule or not piece:
        return 0, 0
    return _close(rule, piece, piece["ok"] + piece["nok"] >= rule["pictures"])


def apply(rule: dict | None, piece: dict | None, ok_pics: int, nok_pics: int, now: float | None = None,
          piece_id=None) -> tuple[int, int, dict | None]:
    """Group new pictures into pieces. Returns (OK pieces, NOK pieces, the
    open piece afterwards). ``piece`` is the open piece from before (None =
    none); ``piece_id`` the value of the rule's group_key in this read."""
    if not rule:
        return ok_pics, nok_pics, None
    now = time.time() if now is None else now
    n = rule["pictures"]
    allowed = _allowed_nok(rule)
    piece = dict(piece) if piece else _empty(now)
    out_ok = out_nok = 0

    def finish(complete: bool):
        nonlocal piece, out_ok, out_nok
        a, b = _close(rule, piece, complete)
        out_ok, out_nok = out_ok + a, out_nok + b
        piece = _empty(now, piece.get("id"))

    # a piece that ran out of time, or a new piece id, ends the open piece
    started = piece.get("since")
    if piece["ok"] + piece["nok"] and rule.get("timeout_s") and started is not None and now - started >= rule["timeout_s"]:
        finish(False)
    if rule.get("group_key") and piece_id is not None:
        if piece["ok"] + piece["nok"] and piece.get("id") != piece_id:
            finish(False)
        piece["id"] = piece_id

    ok_left, nok_left = max(ok_pics, 0), max(nok_pics, 0)
    while ok_left or nok_left:
        # worst case order: a NOK picture goes to a piece that isn't NOK yet
        if nok_left and (piece["nok"] <= allowed or not ok_left):
            nok_left -= 1
            piece["nok"] += 1
        else:
            ok_left -= 1
            piece["ok"] += 1
        if piece["since"] is None:
            piece["since"] = now
        if piece["ok"] + piece["nok"] >= n:
            finish(True)
        elif rule.get("nok_closes") and piece["nok"] > allowed:
            finish(True)  # NOK already: the next picture is the next piece
        if not ok_left and nok_left > 1 and piece["ok"] + piece["nok"] == 0 and not rule.get("group_key"):
            # only NOK pictures left: whole pieces at once (big polled jumps)
            per = n if not rule.get("nok_closes") else allowed + 1
            whole = nok_left // per
            out_nok += whole
            nok_left -= whole * per
        if not nok_left and ok_left > n and piece["ok"] + piece["nok"] == 0:
            whole = ok_left // n  # only OK pictures left: whole OK pieces at once
            out_ok += whole
            ok_left -= whole * n
    open_piece = piece if piece["ok"] + piece["nok"] or piece.get("id") is not None else None
    return out_ok, out_nok, open_piece
