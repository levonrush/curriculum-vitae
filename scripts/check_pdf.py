#!/usr/bin/env python3
"""Validate generated CV and cover-letter PDFs using Poppler tools.

The checks are intentionally ATS-oriented: page geometry, embedded Unicode
fonts, plain-text reading order, URL annotations, metadata, and TeX overflow
warnings.  The script has no Python package dependencies.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"

A4_WIDTH_PT = 595.276
A4_HEIGHT_PT = 841.890
OBSOLETE_DOMAIN = "ldr" + "-data-science"
OBSOLETE_EMAIL = "levon@" + OBSOLETE_DOMAIN

FORBIDDEN_TEXT = {
    OBSOLETE_DOMAIN: "obsolete personal-domain address",
    OBSOLETE_EMAIL: "obsolete domain email",
    "referee": "referee details",
    "references available on request": "reference boilerplate",
    "spearheaded": "prohibited CV cliché",
    "leveraged": "prohibited CV cliché",
    "synergised": "prohibited CV cliché",
    "cutting-edge": "prohibited CV cliché",
    "innovative": "prohibited CV cliché",
    "dynamic professional": "prohibited CV cliché",
    "passionate": "prohibited CV cliché",
    "results-driven": "prohibited CV cliché",
    "thought leader": "prohibited inflated description",
    "visionary": "prohibited inflated description",
    "transformation leader": "prohibited inflated description",
    "transformational": "prohibited inflated description",
    "robust framework": "prohibited CV cliché",
    "strategic alignment": "prohibited CV cliché",
    "best practice": "prohibited CV cliché",
    "actionable insights": "prohibited CV cliché",
    "seamless": "prohibited CV cliché",
    "13 years": "stale years-of-experience wording",
    "buildkite": "unconfirmed tool claim",
    "feature store": "unconfirmed platform claim",
    "production-ready": "unconfirmed production-status claim",
    "first end-to-end": "unsupported first claim",
    "page 4": "unexpected page reference",
}

FORBIDDEN_FONTS = ("aptos", "calibri", "gill sans", "gillsans", "computer modern")
ALLOWED_METADATA = {
    "Author",
    "CreationDate",
    "Creator",
    "Keywords",
    "Producer",
    "Subject",
    "Title",
}


@dataclass
class Result:
    path: Path
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


def command_output(args: list[str], *, check: bool = True) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and completed.returncode:
        raise RuntimeError(f"{' '.join(args)} failed:\n{completed.stdout.strip()}")
    return completed.stdout


def page_bounds(path: Path) -> tuple[int, int]:
    return (1, 1) if "cover_letter" in path.name.lower() else (2, 4)


def reported_page_count(path: Path) -> int:
    info = command_output(["pdfinfo", str(path)])
    match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
    if not match:
        raise RuntimeError(f"pdfinfo did not report a page count for {path}")
    return int(match.group(1))


def inspect_geometry(path: Path, result: Result) -> None:
    info = command_output(["pdfinfo", str(path)])
    page_match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
    size_match = re.search(
        r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts", info, re.MULTILINE
    )
    if not page_match:
        result.error("pdfinfo did not report a page count")
    else:
        page_count = int(page_match.group(1))
        minimum, maximum = page_bounds(path)
        if not minimum <= page_count <= maximum:
            expected = str(minimum) if minimum == maximum else f"{minimum}--{maximum}"
            result.error(f"expected {expected} page(s), found {page_count}")
    if not size_match:
        result.error("pdfinfo did not report page dimensions")
    else:
        width, height = map(float, size_match.groups())
        if abs(width - A4_WIDTH_PT) > 1.0 or abs(height - A4_HEIGHT_PT) > 1.0:
            result.error(f"expected A4, found {width:.2f} x {height:.2f} pt")


def inspect_fonts(path: Path, result: Result) -> None:
    output = command_output(["pdffonts", str(path)])
    rows = output.splitlines()[2:]
    if not rows:
        result.error("pdffonts found no embedded text fonts")
        return
    for row in rows:
        columns = row.split()
        if len(columns) < 8:
            continue
        font_name = columns[0]
        lowered = font_name.lower()
        # emb/sub/uni are the final five-to-three columns before the object id.
        emb, uni = columns[-5], columns[-3]
        if emb != "yes":
            result.error(f"font is not embedded: {font_name}")
        if uni != "yes":
            result.error(f"font lacks a Unicode map: {font_name}")
        if any(name in lowered for name in FORBIDDEN_FONTS):
            result.error(f"unexpected fallback/source font: {font_name}")
        if "ibmplexsans" not in re.sub(r"[^a-z]", "", lowered):
            result.error(f"unexpected document text font: {font_name}")


def extract_text(path: Path, mode: str | None = None) -> str:
    args = ["pdftotext"]
    if mode:
        args.append(mode)
    args.extend([str(path), "-"])
    return command_output(args)


def normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def ordered(text: str, needles: list[str]) -> bool:
    position = -1
    lowered = text.casefold()
    for needle in needles:
        next_position = lowered.find(needle.casefold(), position + 1)
        if next_position < 0:
            return False
        position = next_position
    return True


def inspect_text(path: Path, result: Result) -> None:
    plain = extract_text(path)
    raw = extract_text(path, "-raw")
    layout = extract_text(path, "-layout")
    normalised = normalise_text(plain)

    if "levon rush" not in normalised:
        result.error("candidate name is missing from extracted text")

    for forbidden, reason in FORBIDDEN_TEXT.items():
        if forbidden in normalised:
            result.error(f"contains {reason}: {forbidden!r}")

    is_cover = "cover_letter" in path.name.lower()
    if is_cover:
        required = ["levon rush", "dear", "yours sincerely"]
        for mode_name, text in (("default", plain), ("raw", raw), ("layout", layout)):
            if not ordered(text, required):
                result.error(
                    f"{mode_name} cover extraction order is not name → salutation → closing"
                )
        if "template" not in path.name.lower():
            for marker in (
                "[company]",
                "[role]",
                "company name",
                "role title",
                "todo",
                "placeholder",
            ):
                if marker in normalised:
                    result.error(f"unresolved application placeholder: {marker}")
    else:
        for required in (
            "lead machine learning scientist",
            "hunter water",
            "nib health funds",
            "doctor of philosophy",
            "technical capabilities",
            "community leadership",
        ):
            if required not in normalised:
                result.error(f"required CV content is missing: {required}")
        if "ml_platform" in path.name.lower():
            expected_order = [
                "current experience",
                "platform capability",
                "applied ml portfolio",
                "previous experience",
                "doctoral research",
                "selected publication",
                "education",
                "technical capabilities",
                "community leadership",
            ]
        elif "research_engineer" in path.name.lower():
            expected_order = [
                "current experience",
                "doctoral research",
                "research engineering practice",
                "previous experience",
                "research themes",
                "selected publication",
                "education",
                "technical capabilities",
                "community leadership",
            ]
        else:
            expected_order = [
                "current experience",
                "applied ml portfolio",
                "platform and technical leadership",
                "previous experience",
                "doctoral research",
                "selected publication",
                "education",
                "technical capabilities",
                "community leadership",
            ]
        # Every Poppler extraction mode must retain the same single-column
        # section order and the ATS-critical identity/employer strings.
        for mode_name, text in (("default", plain), ("raw", raw), ("layout", layout)):
            lowered = normalise_text(text)
            for sentinel in ("levon rush", "hunter water", "nib health funds"):
                if sentinel not in lowered:
                    result.error(f"{mode_name} extraction lost {sentinel!r}")
            if not ordered(text, expected_order):
                result.error(f"{mode_name} extraction lost the expected section order")


def inspect_urls(path: Path, result: Result) -> None:
    output = command_output(["pdfinfo", "-url", str(path)])
    urls: list[str] = []
    for line in output.splitlines()[1:]:
        match = re.match(r"^\s*\d+\s+\S+\s+(.+?)\s*$", line)
        if match:
            urls.append(match.group(1))
    for url in urls:
        if not url.startswith(("https://", "mailto:", "tel:")):
            result.error(f"unsupported or malformed URL annotation: {url}")
        if "mailto:http" in url or "mailto:medium" in url:
            result.error(f"malformed mail link: {url}")
        if OBSOLETE_DOMAIN in url.casefold():
            result.error(f"obsolete URL annotation: {url}")
        parsed = urlsplit(url)
        if parsed.scheme == "https" and (not parsed.netloc or " " in url):
            result.error(f"malformed HTTPS URL annotation: {url}")
        elif parsed.scheme == "mailto" and not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+", parsed.path
        ):
            result.error(f"malformed email URL annotation: {url}")
        elif parsed.scheme == "tel" and not re.fullmatch(r"\+[0-9]{8,15}", parsed.path):
            result.error(f"malformed telephone URL annotation: {url}")
    expected_count = 4 if "cover_letter" in path.name.lower() else 5
    if len(urls) != expected_count:
        result.error(f"expected {expected_count} URL annotations, found {len(urls)}")
    result.note(f"{len(urls)} URL annotation(s)")


def inspect_metadata(path: Path, result: Result) -> None:
    output = command_output(["pdfinfo", "-custom", str(path)])
    metadata: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^([^:]+):\s*(.*)$", line)
        if match:
            metadata[match.group(1).strip()] = match.group(2).strip()
    unexpected = sorted(set(metadata) - ALLOWED_METADATA)
    missing = sorted(ALLOWED_METADATA - set(metadata))
    if unexpected:
        result.error(f"metadata contains non-allow-listed keys: {', '.join(unexpected)}")
    if missing:
        result.error(f"metadata is missing allow-listed keys: {', '.join(missing)}")
    if metadata.get("Author") != "Levon Rush":
        result.error("metadata author is not the explicit candidate name")
    if metadata.get("Creator") != "XeLaTeX":
        result.error("metadata creator is not XeLaTeX")
    if not metadata.get("Producer", "").startswith("xdvipdfmx"):
        result.error("metadata producer is not xdvipdfmx")
    if not metadata.get("Title", "").startswith("Levon Rush"):
        result.error("metadata title is outside the title allow-list")
    lowered = output.casefold()
    for leaked in (
        "/users/",
        "downloads/",
        "carrington",
        "grammarly",
        "sensitivity",
        "nib health funds ltd\\",
        OBSOLETE_DOMAIN,
    ):
        if leaked in lowered:
            result.error(f"metadata leaks unexpected value: {leaked}")


def inspect_log(path: Path, result: Result) -> None:
    candidates = [path.with_suffix(".log"), BUILD / f"{path.stem}.log"]
    log_path = next((candidate for candidate in candidates if candidate.exists()), None)
    if not log_path:
        result.note("no matching TeX log found")
        return
    log = log_path.read_text(encoding="utf-8", errors="replace")
    if re.search(r"Overfull \\[hv]box", log):
        result.error(f"overflow warning in {log_path.name}")
    for phrase in (
        "font not found",
        "file not found",
        "undefined control sequence",
        "missing character",
    ):
        if phrase in log.casefold():
            result.error(f"LaTeX log contains {phrase!r}")
    if re.search(r"(?:LaTeX|Package [^\n]+) Warning:", log):
        result.error(f"LaTeX/package warning in {log_path.name}")


def inspect_pgm(path: Path, result: Result) -> None:
    """Apply a small contrast proxy to a greyscale Poppler proof."""
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
    if len(tokens) != 4 or tokens[0] != b"P5" or tokens[3] != b"255":
        result.error(f"could not parse greyscale proof {path.name}")
        return
    while position < len(data) and data[position] in b" \t\r\n":
        position += 1
    pixels = data[position:]
    if not pixels:
        result.error(f"greyscale proof is empty: {path.name}")
        return
    dark_ratio = sum(value < 190 for value in pixels) / len(pixels)
    if max(pixels) - min(pixels) < 100 or dark_ratio < 0.002:
        result.error(f"greyscale proof has insufficient contrast: {path.name}")


def make_proofs(path: Path, proof_dir: Path, result: Result) -> None:
    proof_dir.mkdir(parents=True, exist_ok=True)
    colour_prefix = proof_dir / path.stem
    grey_prefix = proof_dir / f"{path.stem}_Gray"
    command_output(
        ["pdftoppm", "-png", "-r", "120", str(path), str(colour_prefix)]
    )
    command_output(
        ["pdftoppm", "-png", "-gray", "-r", "120", str(path), str(grey_prefix)]
    )
    with tempfile.TemporaryDirectory(prefix="cv-gray-") as temporary:
        pgm_prefix = Path(temporary) / "page"
        command_output(["pdftoppm", "-gray", "-r", "72", str(path), str(pgm_prefix)])
        pgm_paths = sorted(Path(temporary).glob("page-*.pgm"))
        page_count = reported_page_count(path)
        if len(pgm_paths) != page_count:
            result.error(
                f"expected {page_count} greyscale proof page(s), found {len(pgm_paths)}"
            )
        for pgm_path in pgm_paths:
            inspect_pgm(pgm_path, result)
    result.note(f"proofs written to {proof_dir.relative_to(ROOT)}")


def compare_logo_pairs(paths: list[Path], results: dict[Path, Result]) -> None:
    by_name = {path.name: path for path in paths}
    for path in paths:
        if "_No_Logos" in path.stem:
            continue
        no_logo_name = f"{path.stem}_No_Logos.pdf"
        no_logo = by_name.get(no_logo_name)
        if not no_logo:
            continue
        if normalise_text(extract_text(path)) != normalise_text(extract_text(no_logo)):
            results[path].error(f"text differs from {no_logo.name}")
            results[no_logo].error(f"text differs from {path.name}")


def discover_paths(arguments: list[str]) -> list[Path]:
    if arguments:
        paths = [Path(item).expanduser().resolve() for item in arguments]
    else:
        paths = sorted(BUILD.glob("*.pdf"))
    return [path for path in paths if path.suffix.casefold() == ".pdf"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="*", help="PDFs to check; defaults to build/*.pdf")
    parser.add_argument(
        "--proof",
        action="store_true",
        help="also render colour and greyscale PNG proofs under build/proof",
    )
    args = parser.parse_args(argv)

    required_commands = ("pdfinfo", "pdffonts", "pdftotext")
    if args.proof:
        required_commands += ("pdftoppm",)
    missing = [command for command in required_commands if not shutil.which(command)]
    if missing:
        print(f"error: missing required command(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    paths = discover_paths(args.pdf)
    if not paths:
        print("error: no PDF files found", file=sys.stderr)
        return 2
    missing_paths = [path for path in paths if not path.is_file()]
    if missing_paths:
        for path in missing_paths:
            print(f"error: PDF does not exist: {path}", file=sys.stderr)
        return 2

    results = {path: Result(path) for path in paths}
    for path, result in results.items():
        try:
            inspect_geometry(path, result)
            inspect_fonts(path, result)
            inspect_text(path, result)
            inspect_urls(path, result)
            inspect_metadata(path, result)
            inspect_log(path, result)
            if args.proof:
                make_proofs(path, BUILD / "proof", result)
        except (OSError, RuntimeError) as error:
            result.error(str(error))

    compare_logo_pairs(paths, results)

    error_count = 0
    for path, result in results.items():
        status = "PASS" if not result.errors else "FAIL"
        print(f"{status} {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
        for note in result.notes:
            print(f"  note: {note}")
        for error in result.errors:
            print(f"  error: {error}")
        error_count += len(result.errors)

    if error_count:
        print(f"\n{error_count} validation error(s)", file=sys.stderr)
        return 1
    print(f"\nValidated {len(paths)} PDF(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
