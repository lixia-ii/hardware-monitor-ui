"""Dependency-free SVG captcha: glyphs are distorted stroke paths, never <text>, so the answer is not in the markup."""
import math
import secrets

# Polylines on a 10 x 16 grid; only digits that stay distinct when rotated a little.
GLYPHS = {
    "2": [[(0, 4), (2, 1), (5, 0), (8, 1), (10, 4), (9, 7), (0, 16), (10, 16)]],
    "3": [[(0, 1), (4, 0), (8, 1), (9, 4), (8, 7), (4, 8)], [(4, 8), (9, 9), (10, 13), (8, 15), (4, 16), (0, 15)]],
    "4": [[(7, 16), (7, 0), (0, 11), (10, 11)]],
    "5": [[(10, 0), (1, 0), (0, 7), (5, 6), (9, 8), (10, 12), (8, 15), (4, 16), (0, 14)]],
    "6": [[(9, 1), (5, 0), (1, 3), (0, 9), (1, 14), (5, 16), (9, 14), (10, 11), (8, 8), (4, 7), (0, 9)]],
    "7": [[(0, 0), (10, 0), (4, 16)]],
    "8": [[(5, 7), (1, 5), (1, 2), (5, 0), (9, 2), (9, 5), (5, 7), (0, 10), (0, 14), (5, 16), (10, 14), (10, 10), (5, 7)]],
    "9": [[(10, 7), (6, 9), (1, 8), (0, 4), (2, 1), (5, 0), (9, 1), (10, 6), (9, 12), (6, 16), (1, 15)]],
}
ALPHABET = "".join(GLYPHS)
COLORS = ("#78e2b3", "#87adff", "#ffc781", "#ff929b", "#c9a7ff", "#eef2f8")
WIDTH, HEIGHT = 160, 56


def uniform(low, high):
    return low + (high - low) * secrets.randbelow(10001) / 10000


def answer(length=5):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def svg(text):
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}">',
             f'<rect width="{WIDTH}" height="{HEIGHT}" rx="10" fill="#0f1522"/>']
    for _ in range(5):  # Background noise curves cross the glyphs.
        points = [(uniform(0, WIDTH), uniform(0, HEIGHT)) for _ in range(4)]
        d = "M{:.1f},{:.1f} C{:.1f},{:.1f} {:.1f},{:.1f} {:.1f},{:.1f}".format(*sum(points, ()))
        parts.append(f'<path d="{d}" fill="none" stroke="{secrets.choice(COLORS)}" stroke-opacity=".45" stroke-width="{uniform(1, 2.2):.1f}"/>')
    step = (WIDTH - 20) / len(text)
    for index, char in enumerate(text):
        scale, angle = uniform(1.9, 2.4), math.radians(uniform(-22, 22))
        skew = uniform(-.25, .25)
        cx, cy = 10 + step * (index + .5) + uniform(-3, 3), HEIGHT / 2 + uniform(-4, 4)
        def place(x, y):
            x, y = (x - 5 + skew * (y - 8)) * scale, (y - 8) * scale * .95
            x, y = x + uniform(-.7, .7) * scale, y + uniform(-.7, .7) * scale
            return cx + x * math.cos(angle) - y * math.sin(angle), cy + x * math.sin(angle) + y * math.cos(angle)
        color = secrets.choice(COLORS)
        for stroke in GLYPHS[char]:
            d = " ".join(("M" if i == 0 else "L") + "{:.1f},{:.1f}".format(*place(*p)) for i, p in enumerate(stroke))
            parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{uniform(2.6, 3.6):.1f}" stroke-linecap="round" stroke-linejoin="round"/>')
    for _ in range(30):
        parts.append(f'<circle cx="{uniform(0, WIDTH):.1f}" cy="{uniform(0, HEIGHT):.1f}" r="{uniform(.6, 1.6):.1f}" fill="{secrets.choice(COLORS)}" fill-opacity=".6"/>')
    parts.append("</svg>")
    return "".join(parts)
