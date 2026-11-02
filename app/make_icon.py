"""生成 app.ico (风扇图标)。运行一次即可: python make_icon.py"""
import math
from PIL import Image, ImageDraw

S = 1024  # 超采样画布, 缩小后更平滑
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S // 5, fill=(30, 34, 52, 255))
cx = cy = S / 2
d.ellipse((cx - 400, cy - 400, cx + 400, cy + 400), outline=(70, 80, 120, 255), width=28)

# 四片扇叶: 每片是一个偏向一侧的扇形弧
for i in range(4):
    a = i * 90
    d.pieslice((cx - 360, cy - 360, cx + 360, cy + 360), a - 12, a + 52, fill=(88, 196, 255, 255))
    d.pieslice((cx - 90, cy - 90, cx + 90, cy + 90), a - 12, a + 52, fill=(30, 34, 52, 255))

d.ellipse((cx - 90, cy - 90, cx + 90, cy + 90), fill=(235, 240, 255, 255))
d.ellipse((cx - 36, cy - 36, cx + 36, cy + 36), fill=(30, 34, 52, 255))

import os
img.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
