#!/usr/bin/env python3
"""Validate pinned logo assets and their page-level TeX usage."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "assets" / "logo_sources.json"
VENDOR = ROOT / ".vendor" / "logos"
VARIANTS = ROOT / "variants"
COMMANDS = ROOT / "src" / "commands.tex"
SHA256_RE = re.compile(r"[0-9a-f]{64}")

EXPECTED_PLACEMENTS = {
    "hunter_water": {
        "macro": "HunterLogo",
        "source": ROOT / "src" / "content" / "experience_hunter_water.tex",
        "role": re.compile(
            r"\\RoleWithLogo\{Lead Machine Learning Scientist\}"
            r"\{Hunter Water\}\{Intelligent Networks\}"
            r"\{August 2024--present\}\{\\HunterLogo\}"
        ),
        "definition": re.compile(
            r"\\newcommand\{\\HunterLogo\}\{\\InlineLogoImage\{hunter-water\.png\}\}"
        ),
    },
    "nib": {
        "macro": "NibLogo",
        "source": ROOT / "src" / "content" / "experience_nib.tex",
        "role": re.compile(
            r"\\RoleWithLogo\{Lead Data Scientist\}\{nib Health Funds\}"
            r"\{Machine learning\}\{September 2022--January 2024\}\{\\NibLogo\}"
        ),
        "definition": re.compile(
            r"\\newcommand\{\\NibLogo\}\{\\InlineLogoImage\{nib\.pdf\}\}"
        ),
    },
    "university_newcastle": {
        "macro": "UniversityLogo",
        "source": ROOT / "src" / "content" / "experience_nib.tex",
        "role": re.compile(
            r"\\RoleWithLogo\{Student Information Assistant\}"
            r"\{University of Newcastle\}\{Marketing and engagement\}"
            r"\{March 2012--March 2015\}\{\\UniversityLogo\}"
        ),
        "definition": re.compile(
            r"\\newcommand\{\\UniversityLogo\}\{\\InlineUniversityLogo\}"
        ),
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_output(arguments: list[str]) -> str:
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode:
        raise RuntimeError(f"{' '.join(arguments)} failed:\n{completed.stdout.strip()}")
    return completed.stdout


def pdf_dimensions(path: Path) -> tuple[float, float]:
    output = command_output(["pdfinfo", str(path)])
    match = re.search(
        r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts", output, re.MULTILINE
    )
    if not match:
        raise RuntimeError(f"pdfinfo did not report dimensions for {path}")
    return tuple(map(float, match.groups()))


def png_dimensions(path: Path) -> tuple[int, int, int]:
    header = path.read_bytes()[:26]
    if len(header) < 26 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise RuntimeError(f"not a valid PNG with an IHDR header: {path}")
    width, height = struct.unpack(">II", header[16:24])
    colour_type = header[25]
    return width, height, colour_type


def png_chromatic_ratio(path: Path) -> float:
    """Decode an 8-bit, non-interlaced RGB/RGBA PNG using only stdlib."""
    data = path.read_bytes()
    position = 8
    compressed = bytearray()
    width = height = colour_type = bit_depth = interlace = 0
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        chunk_type = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, colour_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif chunk_type == b"IDAT":
            compressed.extend(payload)
        elif chunk_type == b"IEND":
            break
    if bit_depth != 8 or colour_type not in (2, 6) or interlace != 0:
        raise RuntimeError("PNG colour inspection requires non-interlaced 8-bit RGB/RGBA")
    channels = 3 if colour_type == 2 else 4
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise RuntimeError("PNG decompressed size does not match its IHDR dimensions")

    previous = bytearray(stride)
    offset = 0
    chromatic = visible = 0
    for _ in range(height):
        filter_type = raw[offset]
        filtered = raw[offset + 1 : offset + 1 + stride]
        offset += stride + 1
        row = bytearray(stride)
        for index, value in enumerate(filtered):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            else:
                raise RuntimeError(f"unsupported PNG filter type: {filter_type}")
            row[index] = (value + predictor) & 0xFF
        for index in range(0, stride, channels):
            red, green, blue = row[index : index + 3]
            alpha = row[index + 3] if channels == 4 else 255
            if alpha > 8:
                visible += 1
                if max(red, green, blue) - min(red, green, blue) >= 20:
                    chromatic += 1
        previous = row
    return chromatic / visible if visible else 0.0


def ppm_pixels(path: Path) -> bytes:
    data = path.read_bytes()
    tokens: list[bytes] = []
    position = 0
    while len(tokens) < 4 and position < len(data):
        while position < len(data) and data[position] in b" \t\r\n":
            position += 1
        if position < len(data) and data[position] == ord("#"):
            position = data.find(b"\n", position)
            if position < 0:
                break
            continue
        end = position
        while end < len(data) and data[end] not in b" \t\r\n":
            end += 1
        tokens.append(data[position:end])
        position = end
    if len(tokens) != 4 or tokens[0] != b"P6" or tokens[3] != b"255":
        raise RuntimeError(f"could not parse rendered colour proof: {path}")
    while position < len(data) and data[position] in b" \t\r\n":
        position += 1
    return data[position:]


def has_chromatic_colour(path: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="cv-logo-colour-") as temporary:
        prefix = Path(temporary) / "logo"
        command_output(
            [
                "pdftoppm",
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-r",
                "36",
                str(path),
                str(prefix),
            ]
        )
        pixels = ppm_pixels(prefix.with_suffix(".ppm"))
    pixel_count = len(pixels) // 3
    if not pixel_count:
        return False
    chromatic = 0
    for index in range(0, pixel_count * 3, 3):
        red, green, blue = pixels[index : index + 3]
        if max(red, green, blue) - min(red, green, blue) >= 20:
            chromatic += 1
    return chromatic / pixel_count >= 0.001


def validate_manifest() -> tuple[list[dict[str, object]], list[str]]:
    errors: list[str] = []
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [], [f"cannot read {MANIFEST}: {error}"]
    records = manifest.get("logos") if isinstance(manifest, dict) else None
    if not isinstance(records, list) or len(records) != 3:
        return [], ["logo manifest must contain exactly three logo records"]
    identifiers = {str(record.get("id")) for record in records if isinstance(record, dict)}
    if identifiers != set(EXPECTED_PLACEMENTS):
        errors.append(f"unexpected logo identifiers: {', '.join(sorted(identifiers))}")
    return records, errors


def validate_record(record: dict[str, object]) -> list[str]:
    errors: list[str] = []
    identifier = str(record.get("id", "unknown"))
    filename = record.get("filename")
    if not isinstance(filename, str) or Path(filename).name != filename:
        return [f"{identifier}: filename is not a safe basename"]
    target = VENDOR / filename
    if not target.is_file():
        return [f"{identifier}: cached asset is missing: {target}"]
    for field in ("download_sha256", "source_sha256", "sha256"):
        value = str(record.get(field, "")).lower()
        if not SHA256_RE.fullmatch(value):
            errors.append(f"{identifier}: {field} is not a SHA-256 value")
    if SHA256_RE.fullmatch(str(record.get("sha256", "")).lower()):
        actual = sha256(target)
        if actual != str(record["sha256"]).lower():
            errors.append(f"{identifier}: cached asset checksum mismatch")
    for field in ("source_page_url", "guidance_url", "download_url"):
        value = record.get(field)
        if not isinstance(value, str) or not value.startswith("https://"):
            errors.append(f"{identifier}: {field} must be a pinned HTTPS URL")
    if record.get("official_asset") is not True:
        errors.append(f"{identifier}: manifest does not identify the asset as official")

    expected_ratio = record.get("expected_aspect_ratio")
    tolerance = float(record.get("aspect_ratio_tolerance", 0.01))
    try:
        if target.suffix.lower() == ".pdf":
            width, height = pdf_dimensions(target)
        elif target.suffix.lower() == ".png":
            width, height, colour_type = png_dimensions(target)
            if colour_type not in (2, 6):
                errors.append(f"{identifier}: PNG is not stored in RGB/RGBA colour mode")
        else:
            errors.append(f"{identifier}: unsupported cached asset type: {target.suffix}")
            width, height = 0, 1
        if not isinstance(expected_ratio, (int, float)):
            errors.append(f"{identifier}: expected_aspect_ratio is missing")
        elif abs(width / height - float(expected_ratio)) > tolerance:
            errors.append(
                f"{identifier}: aspect ratio {width / height:.4f} differs from "
                f"the pinned {float(expected_ratio):.4f}"
            )
    except (OSError, RuntimeError, ZeroDivisionError) as error:
        errors.append(f"{identifier}: could not inspect dimensions: {error}")

    minimum = record.get("minimum_width_mm")
    display_width = record.get("default_width_mm")
    display_height = record.get("default_height_mm")
    if not isinstance(display_width, (int, float)) or display_width <= 0:
        errors.append(f"{identifier}: default display width is missing or invalid")
    if not isinstance(display_height, (int, float)) or display_height <= 0:
        errors.append(f"{identifier}: default display height is missing or invalid")
    if (
        isinstance(display_width, (int, float))
        and isinstance(display_height, (int, float))
        and isinstance(expected_ratio, (int, float))
        and abs(display_width / display_height - float(expected_ratio)) > tolerance
    ):
        errors.append(
            f"{identifier}: configured display box changes the pinned aspect ratio"
        )
    if (
        isinstance(minimum, (int, float))
        and isinstance(display_width, (int, float))
        and display_width < minimum
    ):
        exception = record.get("minimum_size_exception")
        if not isinstance(exception, str) or not exception.strip():
            errors.append(
                f"{identifier}: default width {display_width:g} mm is below the "
                f"{minimum:g} mm minimum without a documented exception"
            )

    colour_mode = record.get("colour_mode")
    if colour_mode == "full_colour":
        try:
            if target.suffix.lower() == ".pdf":
                colour_present = has_chromatic_colour(target)
            elif target.suffix.lower() == ".png":
                colour_present = png_chromatic_ratio(target) >= 0.001
            else:
                colour_present = False
            if not colour_present:
                errors.append(f"{identifier}: rendered logo has no detectable full-colour pixels")
        except (OSError, RuntimeError) as error:
            errors.append(f"{identifier}: could not inspect colour: {error}")
    elif colour_mode == "official_black_white" and target.suffix.lower() == ".png":
        try:
            if png_chromatic_ratio(target) >= 0.001:
                errors.append(f"{identifier}: official black-and-white mark contains chromatic pixels")
        except (OSError, RuntimeError) as error:
            errors.append(f"{identifier}: could not inspect colour: {error}")
    elif colour_mode not in ("full_colour", "official_black_white"):
        errors.append(f"{identifier}: unknown colour_mode {colour_mode!r}")
    return errors


def validate_tex_usage(records: list[dict[str, object]]) -> list[str]:
    errors: list[str] = []
    by_id = {str(record["id"]): record for record in records}
    commands = COMMANDS.read_text(encoding="utf-8")
    height_match = re.search(
        r"\\newcommand\{\\OrganisationLogoHeight\}\{([0-9.]+)mm\}", commands
    )
    if not height_match:
        errors.append("commands.tex: compact organisation-logo height is not declared")
        rendered_height = None
    else:
        rendered_height = float(height_match.group(1))

    for identifier, placement in EXPECTED_PLACEMENTS.items():
        record = by_id[identifier]
        expected_height = record.get("default_height_mm")
        if (
            rendered_height is not None
            and isinstance(expected_height, (int, float))
            and abs(rendered_height - float(expected_height)) > 0.01
        ):
            errors.append(
                f"{identifier}: TeX height {rendered_height:g} mm differs from "
                f"manifest height {float(expected_height):g} mm"
            )
        definition = placement["definition"]
        if not isinstance(definition, re.Pattern) or len(definition.findall(commands)) != 1:
            errors.append(f"commands.tex: {identifier} asset macro is missing or duplicated")
        source = placement["source"]
        if not isinstance(source, Path):
            errors.append(f"{identifier}: placement source is invalid")
            continue
        content = source.read_text(encoding="utf-8")
        role_pattern = placement["role"]
        if not isinstance(role_pattern, re.Pattern) or len(role_pattern.findall(content)) != 1:
            errors.append(
                f"{source.relative_to(ROOT)}: {identifier} must appear once beside its organisation"
            )

    for variant in sorted(VARIANTS.glob("*.tex")):
        content = variant.read_text(encoding="utf-8")
        pages = re.split(r"\\newpage", content)
        if len(pages) != 3:
            errors.append(f"{variant.name}: expected three explicit page compositions")
            continue
        if "\\PageBrand" in content:
            errors.append(f"{variant.name}: legacy page-top brand header is still present")
        if re.search(r"\\(?:HunterLogo|NibLogo|UniversityLogo)\b", content):
            errors.append(f"{variant.name}: logos must be attached to organisation entries")
        if not re.search(r"\\Hunter(?:Applied|Platform|Research)Experience\b", pages[0]):
            errors.append(f"{variant.name}: page 1 does not invoke the branded Hunter role")
        if not re.search(r"\\NibExperience(?:Standard|Platform)\b", pages[1]):
            errors.append(f"{variant.name}: page 2 does not invoke the branded prior experience")

    cover = (ROOT / "src" / "cover_letter.tex").read_text(encoding="utf-8")
    if re.search(
        r"\\(?:InlineLogoImage|InlineUniversityLogo|HunterLogo|NibLogo|UniversityLogo|RoleWithLogo)\b",
        cover,
    ):
        errors.append("cover letter must not display former-employer or institution logos")
    return errors


def main() -> int:
    missing = [tool for tool in ("pdfinfo", "pdftoppm") if not shutil.which(tool)]
    if missing:
        print(f"error: missing required command(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    records, errors = validate_manifest()
    for record in records:
        if isinstance(record, dict):
            errors.extend(validate_record(record))
    if records:
        errors.extend(validate_tex_usage(records))
    if errors:
        for error in errors:
            print(f"FAIL {error}", file=sys.stderr)
        print(f"\n{len(errors)} asset validation error(s)", file=sys.stderr)
        return 1
    print("PASS logo sources, checksums, colour, aspect ratios, and inline placement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
