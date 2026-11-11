"""Draw the app icon (paper2audio/assets/icon.png): a page whose last lines of text turn
into a sound wave, on a blue rounded square in the GUI's accent color.

Run with `uv run python tools/make_icon.py` after changing the design. Drawn at 4x and
scaled down, for smooth edges; the layout follows macOS's icon grid (an 824 px rounded
square with a soft shadow on a 1024 px canvas).
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZE = 1024
SCALE = 4
OUTPUT = Path(__file__).parent.parent / "paper2audio" / "assets" / "icon.png"
TOP, BOTTOM = (86, 140, 245), (44, 82, 200)  # background gradient, around the accent #4072e5
ACCENT = (64, 114, 229)
LINE = (196, 206, 224)  # the text lines
WAVE = [0.30, 0.55, 0.85, 0.60, 1.00, 0.70, 0.40, 0.75, 0.50, 0.28]  # bar heights


def main() -> None:
    s = SIZE * SCALE
    px = lambda v: int(v * SCALE)  # noqa: E731

    # The rounded square, with a vertical gradient
    inset, side = 100, 824
    gradient = Image.new("RGB", (1, s))
    for y in range(s):
        t = y / (s - 1)
        gradient.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))
    gradient = gradient.resize((s, s))
    mask = Image.new("L", (s, s))
    ImageDraw.Draw(mask).rounded_rectangle([px(inset), px(inset), px(inset + side), px(inset + side)],
                                           radius=px(185), fill=255)

    # Drop shadow of the square
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    shadow = Image.new("L", (s, s))
    ImageDraw.Draw(shadow).rounded_rectangle([px(inset), px(inset + 12), px(inset + side), px(inset + side + 12)],
                                             radius=px(185), fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(px(14)))
    canvas.paste((0, 0, 0, 255), mask=shadow)
    canvas.paste(gradient, mask=mask)

    # The page, with a folded corner and its own shadow
    left, top, right, bottom, fold = 302, 210, 722, 790, 110
    page = [(left, top), (right - fold, top), (right, top + fold), (right, bottom), (left, bottom)]
    radius = 28
    page_mask = Image.new("L", (s, s))
    draw = ImageDraw.Draw(page_mask)
    draw.polygon([(px(x), px(y)) for x, y in page], fill=255)
    # round the three square corners
    corner = Image.new("L", (s, s))
    cd = ImageDraw.Draw(corner)
    for cx, cy in [(left, top), (left, bottom), (right, bottom)]:
        cd.rectangle([px(cx - radius), px(cy - radius), px(cx + radius), px(cy + radius)], fill=255)
    rounded = Image.new("L", (s, s))
    ImageDraw.Draw(rounded).rounded_rectangle([px(left), px(top), px(right), px(bottom)], radius=px(radius), fill=255)
    page_mask = ImageChops.subtract(page_mask, ImageChops.subtract(corner, rounded))

    page_shadow = page_mask.point(lambda v: v * 0.35).filter(ImageFilter.GaussianBlur(px(18)))
    canvas.paste((10, 25, 80, 255), (0, px(14)), mask=ImageChops.multiply(page_shadow, mask)
                 .crop((0, 0, s, s - px(14))))
    canvas.paste((255, 255, 255, 255), mask=page_mask)

    draw = ImageDraw.Draw(canvas)
    # the folded corner
    draw.polygon([(px(right - fold), px(top)), (px(right - fold), px(top + fold - 18)),
                  (px(right - fold + 18), px(top + fold)), (px(right), px(top + fold))], fill=(214, 224, 244))

    # text lines
    x0, line_h, gap = 362, 26, 62
    for i, width in enumerate([230, 300, 300, 190]):
        y = 300 + i * gap
        draw.rounded_rectangle([px(x0), px(y), px(x0 + width), px(y + line_h)], radius=px(line_h / 2), fill=LINE)

    # the sound wave, centered under the text
    bar_w, bar_gap, mid, tallest = 22, 13, 640, 170
    total = len(WAVE) * bar_w + (len(WAVE) - 1) * bar_gap
    x = (left + right) / 2 - total / 2
    for height in WAVE:
        h = max(bar_w, tallest * height)
        draw.rounded_rectangle([px(x), px(mid - h / 2), px(x + bar_w), px(mid + h / 2)], radius=px(bar_w / 2), fill=ACCENT)
        x += bar_w + bar_gap

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    icon = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    icon.save(OUTPUT, optimize=True)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
