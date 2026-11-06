"""The pointer tools on a real desktop through a Mac lease: hold, paced drag, taps, modifiers.

An ordinary role holds the lease; the probe drives the helper through the lease-bound
relay against TextEdit, whose accessibility value shows what the pointer did:

- a held click takes at least its hold and changes nothing;
- a paced drag across lines selects text, and typing replaces the selection;
- a tap sequence reports offsets close to the planned cadence;
- a scroll with a held modifier is accepted;
- whether a shift-held click extends the selection is recorded, not asserted: TextEdit
  has placed the caret instead whether the modifier press went to the process, to the
  session or to the HID level, so a modifier on a posted pointer event is not yet known
  to reach an application.

Run from the repository root with `uv run` against a running firm.
"""

import json
import time

from acceptance_common import (
    acceptance_parser,
    helper_rpc,
    release_if_still_held,
    select_station,
    sleeper_run,
    station_status,
    write_json,
)
from firm_sandbox import Config, Sandbox

ROLE = "pointer-probe"
APP = "/System/Applications/TextEdit.app"
LINES = 25  # 18 bytes a line keeps the whole text inside the helper's 512-byte AX value


def window_text_area(tree, title):
    """The text area node inside the window with this title.

    TextEdit opens, and restores from the station's persistent home, other documents
    beside the one the probe creates; only the probe's own window counts.
    """
    for window in tree.get("children", []):
        if window.get("role") == "AXWindow" and str(window.get("title", "")).startswith(title):
            stack = [window]
            while stack:
                node = stack.pop()
                if node.get("role") == "AXTextArea" and isinstance(node.get("value"), str):
                    return node
                stack.extend(node.get("children", []))
    return None


def main():
    parser = acceptance_parser(__doc__)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)

    firm = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.station = select_station(firm, args.station)
    role = f"{ROLE}-{args.station}"
    station_status(firm, args.station)
    sleeper = sleeper_run(firm, role)
    lease = client = None
    findings = {}
    try:
        lease = firm.mac_acquire(role, "pointer-acceptance", station=args.station)
        if lease["station"] != args.station:
            raise SystemExit(
                f"Mac station {args.station} was not free; the firm granted station "
                f"{lease['station']}"
            )
        client = firm.mac_connect(lease["lease_id"])
        rpc = helper_rpc(client, args.evidence, pause=0.3)

        status = rpc("status")
        assert status["ready"] and status["lease_bound"], status
        pid = rpc("launch_app", path=APP)["pid"]
        # TextEdit restores the documents earlier leases left in the station's
        # persistent home; let them appear, then pick the one new, empty document.
        time.sleep(3)
        existing = {w["window_id"] for w in rpc("list_windows", pid=pid)["windows"]}
        rpc("press_key", pid=pid, key_code=45, modifiers=["command"])
        deadline = time.monotonic() + 15
        window = title = None
        while window is None:
            created = [
                w for w in rpc("list_windows", pid=pid)["windows"]
                if w["window_id"] not in existing and w["title"].startswith("Untitled")
                and w["width"] > 0 and w["height"] > 0
            ]
            if created:
                tree = rpc("inspect_ax", pid=pid, depth=8, max_elements=500)["tree"]
                for candidate in created:
                    node = window_text_area(tree, candidate["title"])
                    if node is not None and node["value"] == "":
                        window, title = candidate["window_id"], candidate["title"]
                        break
            if window is None and time.monotonic() >= deadline:
                raise RuntimeError(f"Expected a new empty TextEdit document; saw {created}")
            if window is None:
                time.sleep(0.3)

        def text_area():
            tree = rpc("inspect_ax", pid=pid, depth=8, max_elements=500)["tree"]
            node = window_text_area(tree, title)
            assert node is not None, f"no text area in TextEdit's window {title!r}"
            return node

        def value():
            return text_area()["value"]

        # Typing is acknowledged when posted, not when consumed: wait for every line.
        # The handle focuses the probe's own document first.
        rpc("type_text", pid=pid, handle=text_area()["handle"],
            text="\n".join(f"row {n:02d} abcdefghij" for n in range(LINES)))
        deadline = time.monotonic() + 60
        while True:
            full = value()
            if full.count("\n") == LINES - 1:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"TextEdit never showed all {LINES} lines: {full!r}")
            time.sleep(0.5)
        shot = rpc("capture_window", pid=pid, window_id=window)
        width, height = shot["width"], shot["height"]
        mid_x, mid_y = width // 2, height // 2

        # A held click: at least its hold, and nothing changes.
        held = rpc("click", pid=pid, window_id=window, x=mid_x, y=mid_y, button="left",
                   hold_ms=1500)
        findings["hold_click_elapsed_s"] = held["_elapsed"]
        assert held["_elapsed"] >= 1.5, held
        assert value() == full

        # A shift-held click would extend the selection from the caret, and typing would
        # replace it. Both clicks land inside the text while the document is still tall.
        rpc("click", pid=pid, window_id=window, x=mid_x, y=mid_y - 100, button="left")
        rpc("click", pid=pid, window_id=window, x=mid_x, y=mid_y + 100, button="left",
            modifiers=["shift"])
        rpc("type_text", pid=pid, text="Q")
        after_shift = value()
        findings["lines_typed"] = full.count("\n") + 1
        findings["lines_after_shift_click"] = after_shift.count("\n") + 1
        findings["shift_click_extended_selection"] = (
            "Q" in after_shift and after_shift.count("\n") < full.count("\n") - 3
        )
        rpc("capture_window", pid=pid, window_id=window)

        # A paced drag from inside the remaining text to below it selects to the end;
        # typing replaces the selection.
        drag = rpc("drag", pid=pid, window_id=window, from_x=mid_x, from_y=mid_y - 120,
                   to_x=mid_x, to_y=mid_y + 120, duration_ms=1500)
        findings["paced_drag_elapsed_s"] = drag["_elapsed"]
        assert drag["_elapsed"] >= 1.5, drag
        rpc("type_text", pid=pid, text="Z")
        after_drag = value()
        findings["lines_after_drag"] = after_drag.count("\n") + 1
        assert "Z" in after_drag and after_drag.count("\n") < after_shift.count("\n") - 2, (
            "the paced drag did not select several lines")
        rpc("capture_window", pid=pid, window_id=window)

        # A tap sequence keeps its cadence.
        planned = [0, 250, 250, 250, 250, 250]
        burst = rpc("tap_sequence", pid=pid, window_id=window,
                    taps=[{"x": mid_x, "y": mid_y, "after_ms": ms} for ms in planned])
        cumulative = [sum(planned[: i + 1]) for i in range(len(planned))]
        findings["tap_offsets_ms"] = burst["offsets_ms"]
        findings["tap_planned_ms"] = cumulative
        assert burst["count"] == len(planned), burst
        drift = [abs(a - b) for a, b in zip(burst["offsets_ms"], cumulative, strict=True)]
        findings["tap_max_drift_ms"] = max(drift)
        assert max(drift) <= 150, drift

        # A scroll with a held modifier is accepted.
        rpc("scroll", pid=pid, window_id=window, x=mid_x, y=mid_y, delta_x=0, delta_y=-40,
            modifiers=["option"])

        # The helper's release quits the app; TextEdit keeps the untitled document in
        # the station's home without asking.
        assert rpc("release")["clean"] is True
        assert rpc("list_apps")["apps"] == []
        client.close()
        client = None
        released = firm.mac_release(lease["lease_id"])
        assert released["clean"] is True, released
        findings["release"] = released
    finally:
        if client is not None:
            client.close()
        release_if_still_held(firm, lease)
        if firm.status(sleeper).get("running"):
            firm.stop(sleeper)
    findings["passed"] = True
    write_json(args.evidence / "result.json", findings)
    print(json.dumps({"passed": True, "evidence": str(args.evidence),
                      "shift_click_extended_selection":
                      findings["shift_click_extended_selection"]}))


if __name__ == "__main__":
    main()
