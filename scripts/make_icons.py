"""Génère l'icône du logiciel : onglet du navigateur et raccourci Windows.

Dessinée en 1024 px puis réduite, pour rester nette jusqu'à 16 px. Exécution
dans l'image (Pillow y est déjà) :

    docker run --rm -u "$(id -u)" -v "$PWD:/w" -w /w transcription-video-audio:test \
        python scripts/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
TOP, BOTTOM = (0x4C, 0x6E, 0xF5), (0x36, 0x4F, 0xC7)   # --accent de l'interface, en dégradé
STATIC = Path(__file__).resolve().parent.parent / "source" / "static"


def draw() -> Image.Image:
    # Fond : carré arrondi en dégradé vertical.
    gradient = Image.new("RGB", (SIZE, SIZE))
    pixels = ImageDraw.Draw(gradient)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        pixels.line([(0, y), (SIZE, y)],
                    fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=230, fill=255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(gradient, mask=mask)

    # Lecture vidéo au-dessus, sous-titres en dessous : vidéo → texte.
    pen = ImageDraw.Draw(icon)
    pen.polygon([(392, 200), (392, 580), (722, 390)], fill="white")
    pen.rounded_rectangle((206, 680, 818, 766), radius=43, fill="white")
    pen.rounded_rectangle((316, 820, 708, 906), radius=43, fill=(255, 255, 255, 190))
    return icon


def main() -> None:
    icon = draw()
    frames = {size: icon.resize((size, size), Image.LANCZOS)
              for size in (16, 20, 24, 32, 40, 48, 64, 128, 180, 256, 512)}
    ico_sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    # Images BMP et non PNG dans l'.ico : le lecteur d'icônes classique de Windows
    # (raccourcis, System.Drawing) affiche du bruit sur les petites tailles en PNG.
    frames[256].save(STATIC / "icon.ico", format="ICO", sizes=[(s, s) for s in ico_sizes],
                     append_images=[frames[s] for s in ico_sizes if s != 256],
                     bitmap_format="bmp")
    frames[32].save(STATIC / "icon-32.png")
    frames[180].save(STATIC / "icon-180.png")          # iOS / raccourci d'écran d'accueil
    frames[512].save(STATIC / "icon-512.png")
    for name in ("icon.ico", "icon-32.png", "icon-180.png", "icon-512.png"):
        print(f"{STATIC / name}  {(STATIC / name).stat().st_size} octets")


if __name__ == "__main__":
    main()
