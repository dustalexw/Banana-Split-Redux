"""Draw the app icon (original artwork) and write PNG + ICNS with Pillow."""
import sys

from PIL import Image, ImageDraw, ImageFilter


def draw(size=1024):
    S = 4  # supersample
    W = size * S
    im = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # rounded-square tile (macOS style margins)
    m = int(W * 0.09)
    d.rounded_rectangle([m, m, W - m, W - m], radius=int(W * 0.2), fill=(32, 34, 44, 255))
    # film strip band across the tile
    band_y0, band_y1 = int(W * 0.60), int(W * 0.76)
    d.rectangle([m, band_y0, W - m, band_y1], fill=(16, 17, 22, 255))
    hole = int(W * 0.028)
    for i in range(9):
        x = m + int(W * 0.05) + i * int((W - 2 * m - W * 0.1) / 8)
        for y in (band_y0 + int(W * 0.015), band_y1 - int(W * 0.015) - hole):
            d.rounded_rectangle([x, y, x + hole, y + hole], radius=hole // 4, fill=(70, 72, 84, 255))
    # the split: a bright vertical gap through the strip
    gap_x = W // 2
    d.rectangle([gap_x - int(W * 0.012), band_y0 - int(W * 0.03), gap_x + int(W * 0.012), band_y1 + int(W * 0.03)],
                fill=(120, 220, 160, 255))
    # banana: thick crescent built from two ellipses
    ban = Image.new("L", (W, W), 0)
    bd = ImageDraw.Draw(ban)
    bd.ellipse([int(W * 0.16), int(W * 0.05), int(W * 0.84), int(W * 0.66)], fill=255)
    bd.ellipse([int(W * 0.16), int(W * -0.03), int(W * 0.84), int(W * 0.52)], fill=0)
    ban = ban.filter(ImageFilter.GaussianBlur(S * 1.5))
    yellow = Image.new("RGBA", (W, W), (250, 214, 64, 255))
    im.paste(yellow, (0, 0), ban)
    # banana tips
    d.ellipse([int(W * 0.155), int(W * 0.33), int(W * 0.205), int(W * 0.38)], fill=(110, 84, 40, 255))
    d.ellipse([int(W * 0.795), int(W * 0.33), int(W * 0.845), int(W * 0.38)], fill=(110, 84, 40, 255))
    # three scoops sitting in the crescent
    for cx, col in ((0.36, (246, 160, 190)), (0.50, (250, 244, 228)), (0.64, (150, 96, 64))):
        r = int(W * 0.085)
        x, y = int(W * cx), int(W * 0.47)
        d.ellipse([x - r, y - r, x + r, y + r], fill=col + (255,))
    im = im.resize((size, size), Image.LANCZOS)
    return im


if __name__ == "__main__":
    out_png, out_icns = sys.argv[1], sys.argv[2]
    img = draw(1024)
    img.save(out_png)
    img.save(out_icns, format="ICNS", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
    print(out_png, out_icns)
