"""Image helpers: downscaling, encoding, and contact-sheet montages.

A 4K screenshot is ~8M pixels; inlined it blows the MCP per-message image budget
(Claude Code rejects images over ~25k tokens) and wastes context. We downscale to a
sane long edge before encoding. Anthropic's API downsamples anything over ~1568px on
the long edge server-side anyway, so sending larger pays ~2.7x the tokens for detail
that is discarded. Default to 1568; pass max_dim=0 to keep native pixels (tiny text).
"""
import io

from PIL import Image, ImageDraw

DEFAULT_MAX_DIM = 1568


def downscale(img: Image.Image, max_dim: int | None = DEFAULT_MAX_DIM) -> tuple[Image.Image, float]:
    """Resize so the long edge <= max_dim. Returns (image, scale) where
    scale = rendered/actual (1.0 if unchanged)."""
    w, h = img.size
    long_edge = max(w, h)
    if not max_dim or long_edge <= max_dim:
        return img, 1.0
    scale = max_dim / long_edge
    new = (max(1, round(w * scale)), max(1, round(h * scale)))
    return img.resize(new, Image.LANCZOS), scale


def encode(img: Image.Image, fmt: str = "png", quality: int = 80) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if fmt == "jpeg":
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg"
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), "image/png"


def contact_sheet(frames: list[Image.Image], labels: list[str] | None = None,
                  cols: int = 3, cell_w: int = 360, pad: int = 6,
                  bg=(18, 18, 18), fg=(235, 235, 235)) -> Image.Image:
    """Tile frames into a single labeled montage so the agent can scan a clip in one
    image instead of N separate ones. Each cell is scaled to cell_w wide."""
    if not frames:
        return Image.new("RGB", (cell_w, cell_w), bg)
    cols = max(1, min(cols, len(frames)))
    # Uniform cell height from the first frame's aspect ratio.
    fw, fh = frames[0].size
    cell_h = max(1, round(cell_w * fh / fw))
    label_h = 18 if labels else 0
    rows = (len(frames) + cols - 1) // cols
    sheet_w = cols * cell_w + (cols + 1) * pad
    sheet_h = rows * (cell_h + label_h) + (rows + 1) * pad
    sheet = Image.new("RGB", (sheet_w, sheet_h), bg)
    draw = ImageDraw.Draw(sheet)
    for i, frame in enumerate(frames):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + label_h + pad)
        thumb = frame.resize((cell_w, cell_h), Image.LANCZOS)
        sheet.paste(thumb, (x, y))
        if labels and i < len(labels):
            draw.text((x + 2, y + cell_h + 2), labels[i], fill=fg)
    return sheet
