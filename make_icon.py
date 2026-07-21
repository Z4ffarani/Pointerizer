"""Generate icon.ico — white sleek pointer in a red-ringed dark circle."""
import math
from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

d.ellipse([32, 32, S - 32, S - 32], fill=(33, 33, 33, 255),
          outline=(236, 236, 236, 255), width=56)

# sleek symmetric dart: tip, upper wing, tail notch, lower wing (pointing left),
# rotated 45° so the tip points to the top-left
shape = [(0.0, 0.0), (1.55, -0.60), (1.18, 0.0), (1.55, 0.60)]
ang = math.radians(45)
c, s = math.cos(ang), math.sin(ang)
pts = [(x * c - y * s, x * s + y * c) for x, y in shape]

xs, ys = [p[0] for p in pts], [p[1] for p in pts]
scale = 384 / max(max(xs) - min(xs), max(ys) - min(ys))

# center the polygon's centroid (visual mass), not its bounding box
a = gx = gy = 0.0
for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
    cr = x0 * y1 - x1 * y0
    a += cr
    gx += (x0 + x1) * cr
    gy += (y0 + y1) * cr
gx, gy = gx / (3 * a), gy / (3 * a)

ox, oy = S / 2 - gx * scale, S / 2 - gy * scale
poly = [(ox + x * scale, oy + y * scale) for x, y in pts]
WHITE = (236, 236, 236, 255)
d.polygon(poly, fill=WHITE)
# soften the corners: wide round-jointed outline over the polygon edges
d.line(poly + poly[:2], fill=WHITE, width=48, joint="curve")

img.save("icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
img.resize((256, 256), Image.LANCZOS).save("icon_preview.png")
print("icon.ico written")
