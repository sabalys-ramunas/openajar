"""The checkbox square in a picture of a check (openajar.node.interaction.checks): a square ring 16 to 34 px wide
that stands out from the picture's background, with nothing inside it and nothing just around it. Cloudflare's,
hCaptcha's and reCAPTCHA's checkboxes are such squares, in light and dark themes, at any place in the widget (a
site may centre the widget in a wider box). Pure Python over Pillow's pixels; a widget picture is small."""

from __future__ import annotations

from typing import Any

MIN_SIDE, MAX_SIDE = 16, 34
RING = 2                # px of the ring that must stand out
CONTRAST = 40           # grey levels from the background
RING_SHARE = 0.85       # of the ring's pixels that stand out
INSIDE_SHARE = 0.04     # of the inside's pixels that may (a tick would)
HALO = 3                # px around the ring that must be mostly background
HALO_SHARE = 0.15


def find(image: Any) -> tuple[float, float, int] | None:
    """(x, y, side) of the checkbox's centre in `image` (a PIL image), or None. The leftmost of the best."""
    grey = image.convert("L")
    width, height = grey.size
    pixels = list(grey.get_flattened_data() if hasattr(grey, "get_flattened_data") else grey.getdata())
    background = sorted(pixels)[len(pixels) // 2]
    marks = [1 if abs(v - background) > CONTRAST else 0 for v in pixels]
    table = [[0] * (width + 1) for _ in range(height + 1)]
    for y in range(height):
        row, base, above, here = 0, y * width, table[y], table[y + 1]
        for x in range(width):
            row += marks[base + x]
            here[x + 1] = above[x + 1] + row

    def count(x0: int, y0: int, x1: int, y1: int) -> int:
        return table[y1][x1] - table[y0][x1] - table[y1][x0] + table[y0][x0]

    best: tuple[tuple[float, int], tuple[float, float, int]] | None = None
    for side in range(MIN_SIDE, MAX_SIDE + 1):
        ring_px = side * side - (side - 2 * RING) ** 2
        inside_px = (side - 8) ** 2
        halo_px = (side + 2 * HALO) ** 2 - side * side
        for y in range(height - side):
            for x in range(width - side):
                outer = count(x, y, x + side, y + side)
                ring = outer - count(x + RING, y + RING, x + side - RING, y + side - RING)
                if ring < RING_SHARE * ring_px:
                    continue
                if count(x + 4, y + 4, x + side - 4, y + side - 4) > INSIDE_SHARE * inside_px:
                    continue
                if x >= HALO and y >= HALO and x + side + HALO <= width and y + side + HALO <= height:
                    halo = count(x - HALO, y - HALO, x + side + HALO, y + side + HALO) - outer
                    if halo > HALO_SHARE * halo_px:
                        continue
                score = (ring / ring_px, -x)
                if best is None or score > best[0]:
                    best = (score, (x + side / 2, y + side / 2, side))
    return best[1] if best else None
