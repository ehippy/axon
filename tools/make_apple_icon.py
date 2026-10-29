"""Render the axon apple-touch-icon from the vector logo (180x180 PNG).

iOS "Add to Home Screen" ignores SVG favicons and masks PNGs with its own
rounded squircle, so unlike web/favicon.svg this variant is full-bleed with no
edge-hugging border stroke. Shapes mirror favicon.svg exactly.
"""
import sys
from PIL import Image, ImageDraw

LOGO = {
    'bg': (0x11, 0x13, 0x12),
    'mark': (0xd6, 0xed, 0x99),
    'ring': (13.5, 18.5, 5.6, 3.2),     # cx, cy, r, stroke
    'stem': (20.7, 11.8, 12.6, 3.2),    # x, y_top, height, width
    'dot': (25.2, 7.4, 2.6),            # cx, cy, r
}
SUPER = 40  # 32 logical units * 40 = 1280px canvas, downsampled for AA


def render(size=180):
    scale = 32 * SUPER / 32
    img = Image.new('RGB', (32 * SUPER, 32 * SUPER), LOGO['bg'])
    draw = ImageDraw.Draw(img)
    cx, cy, r, w = LOGO['ring']
    draw.ellipse([(cx - r) * scale, (cy - r) * scale, (cx + r) * scale, (cy + r) * scale],
                 outline=LOGO['mark'], width=int(round(w * scale)))
    x, y, h, sw = LOGO['stem']
    draw.line([x * scale, y * scale, x * scale, (y + h) * scale],
              fill=LOGO['mark'], width=int(round(sw * scale)), joint='curve')
    for cap in (y, y + h):  # round caps like stroke-linecap=round
        draw.ellipse([(x - sw / 2) * scale, (cap - sw / 2) * scale,
                      (x + sw / 2) * scale, (cap + sw / 2) * scale], fill=LOGO['mark'])
    dx, dy, dr = LOGO['dot']
    draw.ellipse([(dx - dr) * scale, (dy - dr) * scale, (dx + dr) * scale, (dy + dr) * scale],
                 fill=LOGO['mark'])
    return img.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else 'apple-touch-icon.png'
    render(int(sys.argv[2]) if len(sys.argv) > 2 else 180).save(out)
    print(out)
