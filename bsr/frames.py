"""Preview frames (decode only; previews never influence exported media).

Previews are full frames converted to RGB for display. Interlaced material is
shown as decoded (no deinterlacing) so what you see is what the file contains.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Optional

import av
from PIL import Image, ImageDraw

from .media import MediaInfo, open_input


class FrameReader:
    """Keeps one demuxer open for repeated random access (thumbnails, stepping)."""

    def __init__(self, info: MediaInfo):
        self.info = info
        self.c = open_input(info.path)
        self.st = self.c.streams[info.video.index]
        self.st.thread_type = "AUTO"
        self.tb = Fraction(self.st.time_base)

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass

    def frames_around(self, pts: int, before: int = 1, after: int = 1) -> list:
        """Decoded frames: `before` frames presented before pts and `after` frames from pts on."""
        self.c.seek(max(0, int(pts - Fraction(3) / self.tb)), stream=self.st, backward=True, any_frame=False)
        prev = []
        out = []
        for f in self.c.decode(self.st):
            if f.pts is None:
                continue
            if f.pts < pts:
                prev.append(f)
                if len(prev) > before:
                    prev.pop(0)
                continue
            if len(out) >= after:
                break
            out.append(f)
            if len(out) >= after:
                break
        return prev + out

    def frames_at(self, wanted: list) -> dict:
        """Decode the given presentation timestamps (one pass when they are close together)."""
        wanted = sorted(set(int(w) for w in wanted if w is not None))
        out = {}
        if not wanted:
            return out
        groups, cur = [], [wanted[0]]
        for w in wanted[1:]:
            if (w - cur[0]) * self.tb > 20:
                groups.append(cur); cur = [w]
            else:
                cur.append(w)
        groups.append(cur)
        for g in groups:
            self.c.seek(max(0, int(g[0] - Fraction(3) / self.tb)), stream=self.st, backward=True, any_frame=False)
            need = set(g)
            for f in self.c.decode(self.st):
                if f.pts is None:
                    continue
                if int(f.pts) in need:
                    out[int(f.pts)] = f
                    need.discard(int(f.pts))
                if not need or f.pts > g[-1]:
                    break
        return out

    def frame_at(self, pts: int):
        fr = self.frames_around(pts, before=0, after=1)
        return fr[0] if fr else None

    def image(self, frame, width: Optional[int] = None, region=None) -> Image.Image:
        img = frame.to_image()
        sar = self.info.video.sar
        if sar and sar not in ("0/1", "1/1", "1:1"):
            try:
                n, d = (int(x) for x in sar.replace(":", "/").split("/"))
                if n and d:
                    img = img.resize((max(1, round(img.width * n / d)), img.height))
            except Exception:
                pass
        if region is not None:
            d = ImageDraw.Draw(img)
            w, h = img.size
            x0 = int(w * region.left); x1 = w - int(w * region.right)
            y0 = int(h * region.top); y1 = h - int(h * region.bottom)
            d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(255, 210, 0), width=max(1, w // 240))
        if width:
            img = img.resize((width, max(1, round(img.height * width / img.width))))
        return img


def contact_sheet(info: MediaInfo, rows: list[tuple[str, int]], before=2, after=2, width=160) -> Image.Image:
    """rows: (label, pts) -> sheet with frames around each pts (debugging/CLI)."""
    r = FrameReader(info)
    try:
        tiles = []
        for label, pts in rows:
            frs = r.frames_around(pts, before, after)
            tiles.append((label, pts, [(f.pts, r.image(f, width)) for f in frs]))
    finally:
        r.close()
    if not tiles:
        return Image.new("RGB", (10, 10))
    th = max((im.height for _, _, fr in tiles for _, im in fr), default=90)
    cols = before + after
    sheet = Image.new("RGB", (cols * (width + 4) + 150, len(tiles) * (th + 18)), (30, 30, 30))
    d = ImageDraw.Draw(sheet)
    for i, (label, pts, frs) in enumerate(tiles):
        y = i * (th + 18)
        d.text((4, y + 4), label, fill=(255, 255, 255))
        for j, (p, im) in enumerate(frs):
            x = 150 + j * (width + 4)
            sheet.paste(im, (x, y + 14))
            d.text((x + 2, y + 2), f"{float(p * Fraction(info.video.time_base)):.2f}", fill=(255, 220, 0) if p >= pts else (180, 180, 180))
    return sheet
