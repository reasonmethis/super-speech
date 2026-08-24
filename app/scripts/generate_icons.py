from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


APP_ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = APP_ROOT / "build"
DESIGN_DIR = APP_ROOT / "design" / "icon-candidates"
PUBLIC_ICON = APP_ROOT / "public" / "icon.svg"
CANVAS_SIZE = 512
RENDER_SCALE = 2


@dataclass(frozen=True)
class Bar:
    x: int
    y: int
    width: int
    height: int
    opacity: float = 1.0


@dataclass(frozen=True)
class IconVariant:
    slug: str
    label: str
    description: str
    bars: tuple[Bar, ...]


def four_bars(
    minimum_height: int,
    maximum_height: int,
    *,
    baseline: int | None = None,
) -> tuple[Bar, ...]:
    count = 4
    width = 36
    gap = 28
    total_width = count * width + (count - 1) * gap
    left = (CANVAS_SIZE - total_width) // 2
    bars = []
    for index in range(count):
        height = round(
            minimum_height
            + (maximum_height - minimum_height) * index / (count - 1)
        )
        y = (CANVAS_SIZE - height) // 2 if baseline is None else baseline - height
        bars.append(
            Bar(
                left + index * (width + gap),
                y,
                width,
                height,
                0.76 + 0.24 * index / (count - 1),
            )
        )
    return tuple(bars)


A4_VARIANT = IconVariant(
    "a4-rising",
    "A4  Rising",
    "4 centered bars",
    four_bars(72, 228),
)

B4_VARIANT = IconVariant(
    "b4-beam",
    "B4  Beam",
    "4 bars on one baseline, lowered 5%",
    four_bars(72, 204, baseline=356),
)

VARIANTS = (A4_VARIANT, B4_VARIANT)
SELECTED_VARIANT = B4_VARIANT


def icon_svg(variant: IconVariant) -> str:
    bars = "\n".join(
        f'    <rect x="{bar.x}" y="{bar.y}" width="{bar.width}" '
        f'height="{bar.height}" rx="{bar.width // 2}" opacity="{bar.opacity}"/>'
        for bar in variant.bars
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="bg" x1="92" y1="70" x2="420" y2="450" gradientUnits="userSpaceOnUse">
      <stop stop-color="#C4B3FF"/>
      <stop offset="0.52" stop-color="#8C70F4"/>
      <stop offset="1" stop-color="#6045C8"/>
    </linearGradient>
    <linearGradient id="shine" x1="128" y1="96" x2="365" y2="412" gradientUnits="userSpaceOnUse">
      <stop stop-color="#FFFFFF" stop-opacity="0.38"/>
      <stop offset="0.42" stop-color="#FFFFFF" stop-opacity="0"/>
    </linearGradient>
    <filter id="shadow" x="-30%" y="-30%" width="160%" height="170%">
      <feDropShadow dx="0" dy="18" stdDeviation="18" flood-color="#2A176F" flood-opacity="0.4"/>
    </filter>
  </defs>
  <rect x="30" y="30" width="452" height="452" rx="126" fill="url(#bg)"/>
  <rect x="45" y="45" width="422" height="422" rx="111" fill="none" stroke="url(#shine)" stroke-width="8"/>
  <g filter="url(#shadow)" fill="#FFFFFF">
{bars}
  </g>
</svg>
'''


def interpolate(
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    amount: float,
) -> tuple[int, int, int, int]:
    return tuple(round(left + (right - left) * amount) for left, right in zip(start, end)) + (255,)


def render_icon(variant: IconVariant) -> Image.Image:
    scale = RENDER_SCALE
    size = CANVAS_SIZE * scale
    image = Image.new("RGBA", (size, size))
    gradient = Image.new("RGBA", (size, size))
    gradient_draw = ImageDraw.Draw(gradient)
    for y in range(size):
        gradient_draw.line(
            (0, y, size, y),
            fill=interpolate((196, 179, 255), (96, 69, 200), y / (size - 1)),
        )

    tile_mask = Image.new("L", (size, size))
    ImageDraw.Draw(tile_mask).rounded_rectangle(
        (30 * scale, 30 * scale, 482 * scale, 482 * scale),
        radius=126 * scale,
        fill=255,
    )
    image.paste(gradient, mask=tile_mask)

    shine = Image.new("RGBA", (size, size))
    ImageDraw.Draw(shine).rounded_rectangle(
        (45 * scale, 45 * scale, 467 * scale, 467 * scale),
        radius=111 * scale,
        outline=(255, 255, 255, 76),
        width=8 * scale,
    )
    image.alpha_composite(shine)

    shadow = Image.new("RGBA", (size, size))
    shadow_draw = ImageDraw.Draw(shadow)
    for bar in variant.bars:
        shadow_draw.rounded_rectangle(
            (
                bar.x * scale,
                (bar.y + 18) * scale,
                (bar.x + bar.width) * scale,
                (bar.y + bar.height + 18) * scale,
            ),
            radius=bar.width * scale // 2,
            fill=(42, 23, 111, 102),
        )
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18 * scale)))

    marks = Image.new("RGBA", (size, size))
    marks_draw = ImageDraw.Draw(marks)
    for bar in variant.bars:
        marks_draw.rounded_rectangle(
            (
                bar.x * scale,
                bar.y * scale,
                (bar.x + bar.width) * scale,
                (bar.y + bar.height) * scale,
            ),
            radius=bar.width * scale // 2,
            fill=(255, 255, 255, round(255 * bar.opacity)),
        )
    image.alpha_composite(marks)
    return image.resize((CANVAS_SIZE, CANVAS_SIZE), Image.Resampling.LANCZOS)


def write_preview(rendered: dict[str, Image.Image]) -> None:
    columns = min(4, len(VARIANTS))
    rows = (len(VARIANTS) + columns - 1) // columns
    margin = 32
    gap = 22
    card_width = 350
    card_height = 392
    preview = Image.new(
        "RGB",
        (
            margin * 2 + columns * card_width + (columns - 1) * gap,
            margin * 2 + rows * card_height + (rows - 1) * gap,
        ),
        "#0b0d14",
    )
    draw = ImageDraw.Draw(preview)
    label_font = ImageFont.load_default(size=27)
    detail_font = ImageFont.load_default(size=17)
    for index, variant in enumerate(VARIANTS):
        left = margin + index % columns * (card_width + gap)
        top = margin + index // columns * (card_height + gap)
        draw.rounded_rectangle(
            (left, top, left + card_width, top + card_height),
            radius=28,
            fill="#151823",
            outline="#292d3d",
            width=2,
        )
        icon = rendered[variant.slug].resize((250, 250), Image.Resampling.LANCZOS)
        preview.paste(icon, (left + 50, top + 26), icon)
        draw.text((left + 28, top + 292), variant.label, fill="#f3efff", font=label_font)
        draw.text(
            (left + 28, top + 338),
            variant.description,
            fill="#9693a8",
            font=detail_font,
        )
    preview.save(APP_ROOT / "design" / "icon-candidates.png", optimize=True)


def main() -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    candidate_names = {
        f"{variant.slug}{suffix}" for variant in VARIANTS for suffix in (".svg", ".png")
    }
    for candidate_path in DESIGN_DIR.iterdir():
        if (
            candidate_path.suffix in {".svg", ".png"}
            and candidate_path.name not in candidate_names
        ):
            candidate_path.unlink()
    rendered: dict[str, Image.Image] = {}
    for variant in VARIANTS:
        (DESIGN_DIR / f"{variant.slug}.svg").write_text(icon_svg(variant), encoding="utf-8")
        rendered[variant.slug] = render_icon(variant)
        rendered[variant.slug].save(DESIGN_DIR / f"{variant.slug}.png", optimize=True)

    PUBLIC_ICON.write_text(icon_svg(SELECTED_VARIANT), encoding="utf-8")
    rendered[SELECTED_VARIANT.slug].save(PUBLIC_ICON.with_suffix(".png"), optimize=True)
    rendered[SELECTED_VARIANT.slug].save(
        BUILD_DIR / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    rendered[SELECTED_VARIANT.slug].save(BUILD_DIR / "icon.icns", format="ICNS")
    write_preview(rendered)


if __name__ == "__main__":
    main()
