"""
Screenshot redaction script for nable marketing assets.

Usage:
    pip install Pillow pytesseract
    brew install tesseract  # macOS
    python scripts/redact_screenshots.py <screenshot.png>

Replaces sensitive Lambda function names with generic placeholders.

The mapping lives OUTSIDE this repo, in `scripts/redactions.local.json`
(gitignored). A redaction list committed to a public repo publishes exactly
the names it exists to hide: anyone reading the script learns the real
account ID and customer name, which defeats the point of running it.

Create it as {"real string": "placeholder", ...}, for example:

    {
      "111122223333": "123456789012",
      "acme-internal-lambda-prd": "acme-textract-lambda-prd"
    }
"""

from PIL import Image, ImageDraw, ImageFont
import json
import sys
import os
from pathlib import Path

_MAP_FILE = Path(__file__).resolve().parent / "redactions.local.json"


def _load_replacements() -> dict:
    """Real -> placeholder, read from the gitignored local map."""
    if not _MAP_FILE.is_file():
        sys.exit(
            f"No redaction map at {_MAP_FILE}.\n"
            "It is deliberately not committed: publishing the list would disclose\n"
            "the very strings it redacts. Create it as {\"real\": \"placeholder\"}."
        )
    try:
        data = json.loads(_MAP_FILE.read_text())
    except ValueError as e:
        sys.exit(f"{_MAP_FILE} is not valid JSON: {e}")
    if not isinstance(data, dict) or not data:
        sys.exit(f"{_MAP_FILE} must be a non-empty object of real -> placeholder.")
    return data


REPLACEMENTS = _load_replacements()

# Fallback: manual pixel regions to black out if OCR isn't available
# Format: (x1, y1, x2, y2) — approximate for a 1512x982 screenshot
# These cover the four Lambda name bullets in screenshot 2
MANUAL_REGIONS = [
    (60, 630, 340, 658),   # lambda name, row 1 (prd)
    (60, 660, 340, 688),   # lambda name, row 2 (qa)
    (60, 690, 340, 718),   # lambda name, row 3 (stg)
    (60, 720, 340, 748),   # lambda name, row 4 (dev, inferred)
]

REPLACEMENT_LABELS = [
    "acme-textract-lambda-prd",
    "acme-textract-lambda-qa",
    "acme-textract-lambda-stg",
    "acme-textract-lambda-dev",
]

# Colors matching Claude Desktop dark theme
BG_COLOR = (32, 33, 35)      # dark background matching the bullet area
TEXT_COLOR = (189, 147, 249)  # purple-ish mono color matching the screenshot


def redact_with_ocr(img_path: str) -> str:
    """Use pytesseract to find and replace text."""
    try:
        import pytesseract
    except ImportError:
        print("pytesseract not installed. Falling back to manual redaction.")
        return redact_manual(img_path)

    img = Image.open(img_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    n = len(data["text"])
    for i in range(n):
        word = data["text"][i].strip()
        for original, replacement in REPLACEMENTS.items():
            if original in word or word in original:
                x = data["left"][i]
                y = data["top"][i]
                w = data["width"][i]
                h = data["height"][i]
                # Black out the region
                draw.rectangle([x, y, x + w, y + h], fill=BG_COLOR)
                # Write replacement (approximate font size)
                try:
                    font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 13)
                except Exception:
                    font = ImageFont.load_default()
                draw.text((x, y), replacement, fill=TEXT_COLOR, font=font)

    out_path = img_path.replace(".png", "_redacted.png")
    img.save(out_path)
    print(f"Saved: {out_path}")
    return out_path


def redact_manual(img_path: str) -> str:
    """Manual pixel-region redaction — no OCR needed."""
    img = Image.open(img_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 13)
    except Exception:
        font = ImageFont.load_default()

    # Scale regions if image is a different size than expected
    expected_width = 1512
    scale = img.width / expected_width

    for i, (x1, y1, x2, y2) in enumerate(MANUAL_REGIONS):
        sx1, sy1, sx2, sy2 = int(x1*scale), int(y1*scale), int(x2*scale), int(y2*scale)
        draw.rectangle([sx1, sy1, sx2, sy2], fill=BG_COLOR)
        draw.text((sx1 + 2, sy1 + 2), REPLACEMENT_LABELS[i], fill=TEXT_COLOR, font=font)

    out_path = img_path.replace(".png", "_redacted.png")
    img.save(out_path)
    print(f"Saved: {out_path}")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python redact_screenshots.py screenshot.png [screenshot2.png ...]")
        sys.exit(1)

    for path in sys.argv[1:]:
        if not os.path.exists(path):
            print(f"File not found: {path}")
            continue
        print(f"Processing: {path}")
        try:
            redact_with_ocr(path)
        except Exception as e:
            print(f"OCR failed ({e}), using manual redaction")
            redact_manual(path)
