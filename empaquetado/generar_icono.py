"""Genera icono.png e icono.ico (candado sobre documento) para la app."""
import os
from PIL import Image, ImageDraw

AQUI = os.path.dirname(os.path.abspath(__file__))
L = 1024
img = Image.new("RGBA", (L, L), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([40, 40, L - 40, L - 40], radius=200, fill=(33, 94, 150))
# documento
d.rounded_rectangle([250, 170, 690, 780], radius=40, fill=(255, 255, 255))
for y in (280, 360, 440, 520):
    d.rounded_rectangle([320, y, 620, y + 36], radius=18, fill=(190, 205, 222))
# candado
d.rounded_rectangle([540, 560, 860, 850], radius=50, fill=(255, 190, 60))
d.arc([590, 400, 810, 640], start=180, end=360, fill=(255, 190, 60), width=55)
d.ellipse([680, 660, 720, 700], fill=(120, 80, 0))
d.rectangle([690, 690, 710, 770], fill=(120, 80, 0))

img.save(os.path.join(AQUI, "icono.png"))
img.save(os.path.join(AQUI, "icono.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icono generado")
