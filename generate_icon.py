"""Generate the multi-resolution Victus 'V' Windows icon for HANKO Thermal Suite."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

BG_BASE = "#0D0F12"
BORDER_COLOR = "#282D37"
SLATE_POLYGON = "#1C2029"
CYAN_PRIMARY = "#00D8FF"
CYAN_BRIGHT = "#38E4FF"

ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _scale_points(points: list[tuple[float, float]], size: int) -> list[tuple[float, float]]:
    return [(x * size / 100.0, y * size / 100.0) for x, y in points]


def render_victus_emblem(size: int = 1024) -> Image.Image:
    """Render a crisp, high-resolution geometric Victus 'V' icon tile."""
    img = Image.new("RGBA", (size, size), BG_BASE)
    draw = ImageDraw.Draw(img)

    # Outer 1px-equivalent technical frame
    border_w = max(1, round(size * 0.02))
    draw.rectangle(
        [0, 0, size - 1, size - 1],
        outline=BORDER_COLOR,
        width=border_w,
    )

    # Corner registration ticks in matte slate for industrial framing
    tick_len = round(size * 0.08)
    inset = round(size * 0.05)
    tick_w = max(1, round(size * 0.012))
    draw.line([(inset, inset + tick_len), (inset, inset), (inset + tick_len, inset)], fill=SLATE_POLYGON, width=tick_w)
    draw.line(
        [(size - inset - tick_len, size - inset), (size - inset, size - inset), (size - inset, size - inset - tick_len)],
        fill=SLATE_POLYGON,
        width=tick_w,
    )

    # Matte-slate architectural backing polygon
    slate_poly = _scale_points(
        [(10, 18), (35, 18), (50, 58), (65, 18), (90, 18), (57, 89), (43, 89)],
        size,
    )
    draw.polygon(slate_poly, fill=SLATE_POLYGON)

    # Left sweeping steel-cyan blade (#00B4D8)
    left_blade = _scale_points(
        [(15, 14), (32, 14), (50, 61), (50, 86), (46, 86)],
        size,
    )
    draw.polygon(left_blade, fill=CYAN_PRIMARY)

    # Right counter-blade (#38BDF8)
    right_blade = _scale_points(
        [(85, 14), (68, 14), (50, 61), (50, 86), (54, 86)],
        size,
    )
    draw.polygon(right_blade, fill=CYAN_BRIGHT)

    # Center precision join seam
    seam_w = max(1, round(size * 0.012))
    draw.line(
        _scale_points([(50, 61), (50, 86)], size),
        fill=BG_BASE,
        width=seam_w,
    )

    return img


def generate_ico(output_path: Path) -> Path:
    """Render supersampled master artwork and save multi-size .ico file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    master = render_victus_emblem(1024)
    master.save(output_path, format="ICO", sizes=ICON_SIZES)
    return output_path


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent
    ico_path = project_root / "assets" / "hanko_victus.ico"
    result = generate_ico(ico_path)
    print(f"Generated icon: {result} ({result.stat().st_size} bytes)")
