"""Render the README banner from the app's original icon using bundled Qt."""
import base64
import ast
import html
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main():
    root = Path(__file__).resolve().parents[1]
    assets = root / "docs" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    version = next(ast.literal_eval(node.value)
                   for node in ast.parse((root / "bsr/__init__.py").read_text()).body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "ENGINE_VERSION"
                           for target in node.targets))
    version = html.escape(str(version))
    icon = base64.b64encode((root / "bsr_ui/assets/icon.png").read_bytes()).decode()
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="1600" height="460" viewBox="0 0 1600 460">
  <defs>
    <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#20222c"/>
      <stop offset="1" stop-color="#111219"/>
    </linearGradient>
    <linearGradient id="bridgeSky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffe1c5"/>
      <stop offset=".65" stop-color="#ef9475"/>
      <stop offset="1" stop-color="#526e91"/>
    </linearGradient>
    <clipPath id="bridgeCircle"><circle cx="16" cy="16" r="16"/></clipPath>
  </defs>
  <rect width="1600" height="460" rx="28" fill="url(#background)"/>
  <path d="M1510 0V460" stroke="#78dca0" stroke-width="3" opacity=".12"/>
  <path d="M1540 0V460" stroke="#fad640" stroke-width="2" opacity=".08"/>
  <rect x="45" y="45" width="365" height="370" rx="38" fill="#ffffff" opacity=".025"/>
  <image x="48" y="48" width="360" height="360" xlink:href="data:image/png;base64,{icon}"/>
  <g font-family="Arial, Helvetica, sans-serif">
    <rect x="1064" y="56" width="260" height="45" rx="12" fill="#ffffff" fill-opacity=".035" stroke="#343843"/>
    <g transform="translate(1078 62)" clip-path="url(#bridgeCircle)">
      <circle cx="16" cy="16" r="16" fill="url(#bridgeSky)"/>
      <path d="M2.144 24A16 16 0 0 0 29.856 24Q24 23 17 25Q9 22 2.144 24Z" fill="#4d7196"/>
      <g fill="none" stroke="#9f4033" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
        <path d="M9 7V29M23 10V27M7 10H11M7 13H11M21 13H25M21 16H25M3 24L30 22"/>
        <path d="M2 21Q5 18 9 8Q16 24 23 11Q27 19 30 19"/>
        <path d="M4 19V24M13 16V24M17 19V23M20 17V23M27 18V22" stroke-width=".85"/>
      </g>
    </g>
    <text x="1122" y="86" font-size="22" fill="#faf4e4">Only on macOS</text>
    <rect x="1342" y="56" width="128" height="45" rx="22" fill="#f6a0be" fill-opacity=".10"/>
    <text x="1406" y="86" text-anchor="middle" font-size="22" fill="#f6a0be">v{version}</text>
    <text x="462" y="89" font-size="19" font-weight="bold" letter-spacing="4" fill="#78dca0">DESKTOP VIDEO SPLITTER</text>
    <text x="456" y="183" font-size="86" font-weight="bold" fill="#faf4e4">Banana Split <tspan fill="#fad640">Redux</tspan></text>
    <text x="462" y="246" font-size="30" fill="#d1d0d7">Precise video splits. Minimal re-encoding.</text>
    <text x="462" y="291" font-size="25" fill="#a6a7b5">Watch, review, name your parts, and finish.</text>
    <g fill="#ffffff" fill-opacity=".025" stroke="#343843" stroke-width="1">
      <rect x="462" y="335" width="280" height="52" rx="12"/>
      <rect x="760" y="335" width="280" height="52" rx="12"/>
      <rect x="1058" y="335" width="280" height="52" rx="12"/>
    </g>
    <g fill="none" stroke="#fad640" stroke-width="1.8" stroke-linejoin="round">
      <rect x="478" y="350" width="17" height="17" rx="3"/>
      <path d="M484 348V345H501V362H498"/>
    </g>
    <text x="515" y="368" font-size="21" fill="#e3e2e8">Copy when possible</text>
    <g fill="none" stroke="#f6a0be" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="780" cy="356" r="3"/>
      <circle cx="780" cy="366" r="3"/>
      <path d="M783 358L796 350M783 364L796 372"/>
    </g>
    <text x="813" y="368" font-size="21" fill="#e3e2e8">Encode near cuts</text>
    <g fill="none" stroke="#78dca0" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="1086" cy="361" r="11"/>
      <path d="M1081 361L1085 365L1092 357"/>
    </g>
    <text x="1111" y="368" font-size="21" fill="#e3e2e8">Validated exports</text>
  </g>
</svg>'''
    (assets / "readme-banner.svg").write_text(svg)
    app = QGuiApplication([])
    renderer = QSvgRenderer(svg.encode())
    if not renderer.isValid():
        raise RuntimeError("Invalid banner SVG")
    canvas = QImage(1600, 460, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(0)
    painter = QPainter(canvas)
    renderer.render(painter)
    painter.end()
    if not canvas.save(str(assets / "readme-banner.png")):
        raise RuntimeError("Could not save banner PNG")
    print(assets / "readme-banner.png")


if __name__ == "__main__":
    main()
