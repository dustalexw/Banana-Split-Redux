"""Command-line front end (also used for headless testing).

    python -m bsr.cli probe FILE
    python -m bsr.cli analyze FILE [--preset balanced] [--region 0.04,0.04,0.04,0.04]
    python -m bsr.cli export FILE --out DIR --accept 1,3 [--position middle] [--remove-black]
                         [--manual 50.0,70.0] [--parts 2,3] [--choice 3=boundary:12345]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .detect import AnalysisRegion
from .export import ExportOptions, export_plan, default_streams, preservation_report, output_format
from .media import fmt_time
from .session import SourceSession


def _session(a) -> SourceSession:
    s = SourceSession(a.file)
    if a.preset:
        s.set_preset(a.preset)
    if getattr(a, "region", None):
        l, t, r, b = (float(x) for x in a.region.split(","))
        s.scan.region = AnalysisRegion(l, t, r, b)
    if getattr(a, "decimate", None):
        s.scan.decimate = a.decimate
    if getattr(a, "drop_tolerance", None) is not None:
        s.policy.max_auto_drop_black_frames = a.drop_tolerance
    return s


def _prog(label):
    last = [0.0]

    def f(x):
        now = time.time()
        if now - last[0] > 2 or x >= 1:
            last[0] = now
            print(f"  {label} {x*100:5.1f}%", file=sys.stderr, flush=True)
    return f


def cmd_probe(a):
    s = SourceSession(a.file)
    i = s.info
    print(json.dumps(i.to_json(), indent=1, ensure_ascii=False, default=str))
    s.index(progress=_prog("index"))
    print(json.dumps({"gop": s.finder.gop_stats()}, indent=1))


def print_candidates(s: SourceSession, show_all: bool):
    tb = s.vtb
    s.resolve()
    print(f"\n{'id':>3} {'start':>12} {'end':>12} {'frames':>6} {'kind':>9} {'score':>5} {'audio dB':>8}  state     copy boundary")
    for c in s.candidates:
        if c.suppressed and not show_all:
            continue
        d = s.decisions[c.id]
        rc = s.resolved.get(c.id)
        rs = ""
        if rc:
            inside = rc.inside
            ins = ", ".join(f"{fmt_time(o.seconds)}[{'✓' if o.verified else '✗'}{'' if not o.dropped_pts else ' drop'+str(len(o.dropped_pts))}]" for o in inside) or "none inside"
            rs = f"{ins}"
        flag = "suppressed" if c.suppressed else ("lead" if c.leading else ("trail" if c.trailing else ""))
        print(f"{c.id:>3} {fmt_time(c.start_s):>12} {fmt_time(c.end_s):>12} {c.n_frames:>6} {c.kind:>9} {c.score:>5} "
              f"{'' if c.audio_db is None else c.audio_db:>8}  {d.state:<9} {rs} {flag}")


def cmd_analyze(a):
    s = _session(a)
    t0 = time.time()
    s.index(progress=_prog("index"))
    s.analyze(progress=_prog("analyse"))
    print(f"analysed {len(s.stats)} frames in {s.stats.elapsed_s:.1f}s (range {s.stats.range_used}, "
          f"{s.stats.bit_depth}-bit, region px {s.stats.region_px})", file=sys.stderr)
    s.find()
    print(json.dumps({"gop": s.finder.gop_stats(), "preset": s.preset, "detect": s.detect.to_json()}, indent=1))
    for d in s.decisions.values():
        if d.state == "proposed":
            d.state = "accepted"
    print_candidates(s, a.all)
    if a.verbose:
        for k, rc in s.resolved.items():
            if rc.status == "rejected":
                continue
            print(f"\n[{k}] {rc.status}: {rc.label}")
            for m in rc.messages:
                print("    ·", m)
            for ch in rc.choices:
                print("    ?", ch["choice"], "—", ch["label"])
    print(f"\ntotal {time.time()-t0:.1f}s", file=sys.stderr)


def cmd_export(a):
    s = _session(a)
    s.index(progress=_prog("index"))
    s.analyze(progress=_prog("analyse"))
    s.find()
    acc = set(int(x) for x in a.accept.split(",")) if a.accept else set()
    for d in s.decisions.values():
        d.state = "accepted" if d.id in acc else "rejected"
        d.position = a.position
        d.remove_black = a.remove_black
    for ch in a.choice or []:
        k, v = ch.split("=", 1)
        s.decisions[int(k)].choice = v
    if a.manual:
        for t in a.manual.split(","):
            s.add_manual(float(t))
    if a.names:
        s.names = [n.strip() for n in a.names.split("|")]
    plan = s.plan()
    for k, rc in s.resolved.items():
        if rc.status != "rejected":
            print(f"cut {k}: {rc.status} — {rc.label}; " + " | ".join(rc.messages), file=sys.stderr)
            for ch in rc.choices:
                print(f"    option {k}={ch['choice']}: {ch['label']}", file=sys.stderr)
    for sg in plan.segments:
        print(f"part {sg.n}: {fmt_time(sg.start_s)} – {fmt_time(sg.end_s)} ({sg.duration_s:.3f}s) {sg.name}", file=sys.stderr)
    if plan.issues:
        print("PLAN ISSUES:\n  " + "\n  ".join(plan.issues), file=sys.stderr)
        sys.exit(2)
    streams = default_streams(s.info)
    for m in preservation_report(s.info, streams, output_format(s.info)[0]):
        print("note:", m, file=sys.stderr)
    only = [int(x) for x in a.parts.split(",")] if a.parts else None
    rep = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=a.out, streams=streams),
                      progress=lambda st, f: None, log=lambda m: print(m, file=sys.stderr), only=only)
    for o in rep["outputs"]:
        print(f"\n== part {o['n']} {'OK' if o['ok'] else 'FAIL'} {o['path']}")
        for name, c in o["validation"].get("checks", {}).items():
            print(f"   [{'✓' if c['ok'] else '✗'}] {name}: {c['detail']}")
        if o["error"]:
            print("   error:", o["error"])
    print("\nreport:", rep["report_path"])
    sys.exit(0 if rep["ok"] else 1)


def cmd_sheet(a):
    """Contact sheet of frames around every candidate (for quick visual review)."""
    from .frames import contact_sheet
    s = _session(a)
    s.index(); s.analyze(); s.find(audio_signal=False)
    rows = []
    for c in s.candidates:
        if c.suppressed and not a.all:
            continue
        rows.append((f"#{c.id} start {c.start_s:.2f}", c.start_pts))
        rows.append((f"#{c.id} end {c.end_s:.2f}", c.end_pts))
    contact_sheet(s.info, rows, before=2, after=3, width=a.width).save(a.output)
    print(a.output)


def main(argv=None):
    p = argparse.ArgumentParser(prog="bsr")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("probe", "analyze", "export", "sheet"):
        q = sub.add_parser(name)
        q.add_argument("file")
        q.add_argument("--preset", default=None)
        q.add_argument("--region", default=None)
        q.add_argument("--decimate", type=int, default=None)
        q.add_argument("--drop-tolerance", type=int, default=None)
        if name == "sheet":
            q.add_argument("--output", required=True)
            q.add_argument("--all", action="store_true")
            q.add_argument("--width", type=int, default=128)
        if name == "analyze":
            q.add_argument("--all", action="store_true")
            q.add_argument("-v", "--verbose", action="store_true")
        if name == "export":
            q.add_argument("--out", required=True)
            q.add_argument("--accept", default="")
            q.add_argument("--position", default="middle", choices=["start", "middle", "end"])
            q.add_argument("--remove-black", action="store_true")
            q.add_argument("--manual", default="")
            q.add_argument("--parts", default="")
            q.add_argument("--choice", action="append")
            q.add_argument("--names", default="", help="part names separated by |")
    a = p.parse_args(argv)
    {"probe": cmd_probe, "analyze": cmd_analyze, "export": cmd_export, "sheet": cmd_sheet}[a.cmd](a)


if __name__ == "__main__":
    main()
