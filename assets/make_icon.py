"""生成应用图标 assets/mmsearch.ico（python assets/make_icon.py）。"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# 渐变圆角方形底
grad = Image.new("RGBA", (S, S))
top, bottom = (59, 130, 246), (139, 92, 246)
for y in range(S):
    t = y / (S - 1)
    c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
    ImageDraw.Draw(grad).line([(0, y), (S, y)], fill=c)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
# 放大镜
cx, cy, r, w = 440, 430, 230, 70
d.line([(cx + 170, cy + 170), (cx + 360, cy + 360)], fill="white", width=110)
d.ellipse([cx + 305, cy + 305, cx + 415, cy + 415], fill="white")  # 圆头
d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="white", width=w)
# 镜片里的四种模态色块
colors = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6"]
d.ellipse([cx - r + w, cy - r + w, cx + r - w, cy + r - w], fill=(255, 255, 255, 235))
q, g = 62, 18
for i, c in enumerate(colors):
    x = cx - q - g // 2 + (i % 2) * (q + g)
    y = cy - q - g // 2 + (i // 2) * (q + g)
    d.rounded_rectangle([x, y, x + q, y + q], radius=16, fill=c)

out = Path(__file__).with_name("mmsearch.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(out.with_suffix(".png"))
print("written", out)
