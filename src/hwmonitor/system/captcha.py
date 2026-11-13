"""Dependency-free raster captcha; only flattened PNG pixels leave the server."""
import math
import secrets
import struct
import zlib

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


def png(text):
    pixels = bytearray((15, 21, 34)) * (WIDTH * HEIGHT)

    def line(start, end, color, width, opacity=1):
        """Rasterize a round-ended segment with antialiased edges."""
        ax, ay = start
        bx, by = end
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        radius = width / 2
        rgb = tuple(bytes.fromhex(color[1:]))
        for y in range(max(0, math.floor(min(ay, by) - radius - 1)), min(HEIGHT, math.ceil(max(ay, by) + radius + 1))):
            for x in range(max(0, math.floor(min(ax, bx) - radius - 1)), min(WIDTH, math.ceil(max(ax, bx) + radius + 1))):
                t = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / length)) if length else 0
                distance = math.hypot(x - ax - t * dx, y - ay - t * dy)
                alpha = max(0, min(1, radius + .5 - distance)) * opacity
                if alpha:
                    offset = (y * WIDTH + x) * 3
                    for channel, value in enumerate(rgb):
                        old = pixels[offset + channel]
                        pixels[offset + channel] = round(old + (value - old) * alpha)

    def curve():
        points = [(uniform(0, WIDTH), uniform(0, HEIGHT)) for _ in range(4)]
        color, width = secrets.choice(COLORS), uniform(1, 2.2)
        previous = points[0]
        for i in range(1, 33):
            t = i / 32
            weights = ((1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3)
            current = tuple(sum(w * p[axis] for w, p in zip(weights, points)) for axis in (0, 1))
            line(previous, current, color, width, .45)
            previous = current

    for _ in range(5):  # Background noise curves cross the glyphs.
        curve()
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
            points = [place(*p) for p in stroke]
            width = uniform(2.6, 3.6)
            for start, end in zip(points, points[1:]):
                line(start, end, color, width)
    for _ in range(30):
        point = (uniform(0, WIDTH), uniform(0, HEIGHT))
        line(point, point, secrets.choice(COLORS), uniform(1.2, 3.2), .6)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    # RGB, 8 bits per channel, no metadata or separate glyph objects.
    rows = b"".join(b"\0" + pixels[y * WIDTH * 3:(y + 1) * WIDTH * 3] for y in range(HEIGHT))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
