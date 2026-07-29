from __future__ import annotations

from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SCALE = 4
INK = (35, 51, 45, 255)
WHITE = (255, 255, 255, 255)
HEADER = (23, 53, 43, 255)
ACCENT = (24, 121, 78, 255)


def point(value: float, size: int, inset: float = 0) -> int:
    return round((inset + value * (size - inset * 2) / 24) * SCALE)


def width(value: float) -> int:
    return max(1, round(value * SCALE))


def render(
    draw_icon: Callable[[ImageDraw.ImageDraw, int, tuple[int, int, int, int]], None],
    color: tuple[int, int, int, int],
    size: int = 18,
) -> Image.Image:
    canvas = Image.new("RGBA", (size * SCALE, size * SCALE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw_icon(draw, size, color)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def line(draw: ImageDraw.ImageDraw, size: int, coords: list[tuple[float, float]], color: tuple[int, int, int, int]) -> None:
    draw.line(
        [(point(x, size), point(y, size)) for x, y in coords],
        fill=color,
        width=width(2),
        joint="curve",
    )


def send(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int, int]) -> None:
    line(draw, size, [(22, 2), (15, 22), (11, 13), (2, 9), (22, 2)], color)
    line(draw, size, [(22, 2), (11, 13)], color)


def square(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(
        (point(3, size), point(3, size), point(21, size), point(21, size)),
        radius=point(2, size),
        outline=color,
        width=width(2),
    )


def refresh(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int, int]) -> None:
    box = (point(3, size), point(3, size), point(21, size), point(21, size))
    draw.arc(box, start=205, end=390, fill=color, width=width(2))
    draw.arc(box, start=25, end=210, fill=color, width=width(2))
    line(draw, size, [(3, 3), (3, 8), (8, 8)], color)
    line(draw, size, [(21, 21), (21, 16), (16, 16)], color)


def save(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int, int]) -> None:
    line(draw, size, [(5, 3), (16, 3), (21, 8), (21, 21), (3, 21), (3, 3), (5, 3)], color)
    line(draw, size, [(7, 3), (7, 8), (16, 8), (16, 3)], color)
    draw.rounded_rectangle(
        (point(7, size), point(13, size), point(17, size), point(21, size)),
        radius=point(1.5, size),
        outline=color,
        width=width(2),
    )


def copy_icon(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(
        (point(3, size), point(3, size), point(16, size), point(16, size)),
        radius=point(2, size),
        outline=color,
        width=width(2),
    )
    draw.rounded_rectangle(
        (point(8, size), point(8, size), point(21, size), point(21, size)),
        radius=point(2, size),
        outline=color,
        width=width(2),
    )


def download(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int, int]) -> None:
    line(draw, size, [(12, 3), (12, 15)], color)
    line(draw, size, [(7, 10), (12, 15), (17, 10)], color)
    line(draw, size, [(5, 21), (19, 21)], color)


def rotate_ccw(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int, int]) -> None:
    box = (point(3, size), point(3, size), point(21, size), point(21, size))
    draw.arc(box, start=35, end=320, fill=color, width=width(2))
    line(draw, size, [(3, 3), (3, 9), (9, 9)], color)


def trash_2(draw: ImageDraw.ImageDraw, size: int, color: tuple[int, int, int, int]) -> None:
    line(draw, size, [(3, 6), (21, 6)], color)
    line(draw, size, [(8, 6), (9, 3), (15, 3), (16, 6)], color)
    line(draw, size, [(6, 6), (7, 21), (17, 21), (18, 6)], color)
    line(draw, size, [(10, 11), (10, 17)], color)
    line(draw, size, [(14, 11), (14, 17)], color)


def save_icon(name: str, painter: Callable[[ImageDraw.ImageDraw, int, tuple[int, int, int, int]], None], color: tuple[int, int, int, int]) -> None:
    render(painter, color).save(ASSETS / name, optimize=True)


def build_app_icon() -> None:
    large = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(large)
    draw.rounded_rectangle((8, 8, 248, 248), radius=50, fill=HEADER)
    draw.rounded_rectangle((24, 24, 232, 232), radius=40, outline=ACCENT, width=5)
    glyph = render(send, WHITE, 150)
    large.alpha_composite(glyph, ((256 - glyph.width) // 2, (256 - glyph.height) // 2))
    large.save(ASSETS / "app-256.png", optimize=True)
    large.resize((32, 32), Image.Resampling.LANCZOS).save(ASSETS / "app-32.png", optimize=True)
    large.save(
        ASSETS / "app.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    # The geometry follows the corresponding Lucide icons at a 24x24 viewBox.
    save_icon("send-light.png", send, WHITE)
    save_icon("square-dark.png", square, INK)
    save_icon("refresh-dark.png", refresh, INK)
    save_icon("save-dark.png", save, INK)
    save_icon("copy-dark.png", copy_icon, INK)
    save_icon("download-dark.png", download, INK)
    save_icon("rotate-ccw-dark.png", rotate_ccw, INK)
    save_icon("trash-2-dark.png", trash_2, INK)
    build_app_icon()


if __name__ == "__main__":
    main()
