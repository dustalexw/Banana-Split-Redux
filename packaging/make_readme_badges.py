"""Create rectangular README badges using the app palette and current version."""
import ast
import html
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main():
    root = Path(__file__).resolve().parents[1]
    assets = root / "docs/assets"
    assets.mkdir(parents=True, exist_ok=True)
    version = next(ast.literal_eval(node.value)
                   for node in ast.parse((root / "bsr/__init__.py").read_text()).body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "ENGINE_VERSION"
                           for target in node.targets))
    badges = [
        ("release", "RELEASE", f"V{version}", 84, 82, "#fad640"),
        ("download", "DOWNLOAD", "MACOS APP", 104, 118, "#78dca0"),
        ("platform", "PLATFORM", "MACOS 14+ · APPLE SILICON", 100, 246, "#f6a0be"),
    ]
    app = QGuiApplication([])
    for name, label, value, label_width, value_width, color in badges:
        width = label_width + value_width
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="34" viewBox="0 0 {width} 34" role="img" aria-label="{html.escape(label + ': ' + value)}">
  <title>{html.escape(label + ': ' + value)}</title>
  <rect width="{label_width}" height="34" fill="#20222c"/>
  <rect x="{label_width}" width="{value_width}" height="34" fill="{color}"/>
  <g font-family="Arial" font-size="11" font-weight="bold" letter-spacing="1.2" text-anchor="middle">
    <text x="{label_width / 2}" y="21" fill="#faf4e4">{html.escape(label)}</text>
    <text x="{label_width + value_width / 2}" y="21" fill="#20222c">{html.escape(value)}</text>
  </g>
</svg>'''
        (assets / f"badge-{name}.svg").write_text(svg)
        renderer = QSvgRenderer(svg.encode())
        if not renderer.isValid():
            raise RuntimeError(f"Invalid {name} badge")
        canvas = QImage(width * 2, 68, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(0)
        painter = QPainter(canvas)
        renderer.render(painter)
        painter.end()
        if not canvas.save(str(assets / f"badge-{name}.png")):
            raise RuntimeError(f"Could not save {name} badge")
        print(assets / f"badge-{name}.svg")


if __name__ == "__main__":
    main()
