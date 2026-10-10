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
  </defs>
  <rect width="1600" height="460" rx="28" fill="url(#background)"/>
  <path d="M1510 0V460" stroke="#78dca0" stroke-width="3" opacity=".12"/>
  <path d="M1540 0V460" stroke="#fad640" stroke-width="2" opacity=".08"/>
  <rect x="45" y="45" width="365" height="370" rx="38" fill="#ffffff" opacity=".025"/>
  <image x="48" y="48" width="360" height="360" xlink:href="data:image/png;base64,{icon}"/>
  <g font-family="Arial, Helvetica, sans-serif">
    <rect x="1092" y="56" width="232" height="45" rx="22" fill="#78dca0" fill-opacity=".10"/>
    <text x="1208" y="86" text-anchor="middle" font-size="22" fill="#78dca0">Only on macOS</text>
    <rect x="1342" y="56" width="128" height="45" rx="22" fill="#f6a0be" fill-opacity=".10"/>
    <text x="1406" y="86" text-anchor="middle" font-size="22" fill="#f6a0be">v{version}</text>
    <text x="462" y="89" font-size="19" font-weight="bold" letter-spacing="4" fill="#78dca0">DESKTOP VIDEO SPLITTER</text>
    <text x="456" y="183" font-size="86" font-weight="bold" fill="#faf4e4">Banana Split <tspan fill="#fad640">Redux</tspan></text>
    <text x="462" y="246" font-size="30" fill="#d1d0d7">Precise video splits. Minimal re-encoding.</text>
    <text x="462" y="291" font-size="25" fill="#a6a7b5">Watch, review, name your parts, and finish.</text>
    <rect x="462" y="335" width="218" height="47" rx="23" fill="#fad640" fill-opacity=".10"/>
    <text x="486" y="366" font-size="21" fill="#fad640">Copy when possible</text>
    <rect x="698" y="335" width="220" height="47" rx="23" fill="#f6a0be" fill-opacity=".10"/>
    <text x="722" y="366" font-size="21" fill="#f6a0be">Encode near cuts</text>
    <rect x="936" y="335" width="227" height="47" rx="23" fill="#78dca0" fill-opacity=".10"/>
    <text x="960" y="366" font-size="21" fill="#78dca0">Validated exports</text>
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
