"""Opt-in checks on your own episodes (read-only).

    BSR_REAL_DIR="/path/to/episodes" python -m pytest tests/test_real_files.py -s

For every video file: analyse, prove copy boundaries for the strongest candidates, and
export + validate ONE short part (between the first two accepted copy cuts) to a temp dir.
"""
import hashlib
import os

import pytest

from bsr.export import ExportOptions, export_plan
from bsr.planner import Policy
from bsr.session import SourceSession

REAL = os.environ.get("BSR_REAL_DIR")
EXT = (".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts")
files = sorted(os.path.join(REAL, f) for f in os.listdir(REAL) if f.lower().endswith(EXT)) if REAL else []


def _sha1_head(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        h.update(f.read(1 << 24))
    return h.hexdigest(), os.path.getsize(p), os.path.getmtime(p)


@pytest.mark.skipif(not files, reason="set BSR_REAL_DIR to a folder of episodes")
@pytest.mark.parametrize("path", files, ids=[os.path.basename(f)[:40] for f in files])
def test_real_episode_copy_part(path, tmp_path):
    before = _sha1_head(path)
    s = SourceSession(path)
    s.policy = Policy(max_auto_drop_black_frames=2)
    s.index(); s.analyze(); s.find()
    s.resolve()
    ok = sorted([k for k, rc in s.resolved.items() if rc.status in ("copy", "copy-drop")
                 and not s.candidates[k].leading and not s.candidates[k].trailing],
                key=lambda k: s.candidates[k].start_s)
    print(f"\n{os.path.basename(path)}: {len(s.candidates)} candidates, {len(ok)} copy-resolvable, GOP {s.finder.gop_stats()}")
    if len(ok) < 2:
        pytest.skip("fewer than two copy-resolvable cuts")
    for d in s.decisions.values():
        d.state = "accepted" if d.id in ok[:2] else "rejected"
    plan = s.plan()
    assert not plan.issues
    rep = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=str(tmp_path)), only=[2])
    for o in rep["outputs"]:
        for name, c in o["validation"]["checks"].items():
            print(f"  [{'✓' if c['ok'] else '✗'}] {name}: {c['detail']}")
    assert rep["ok"]
    assert _sha1_head(path) == before
