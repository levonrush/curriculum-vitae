#!/usr/bin/env python3
"""Dependency-free local command line interface for the CV repository."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Sequence, TextIO
import urllib.error
import urllib.request
import zipfile


VARIANTS = {
    "applied": ("applied_scientist", "Applied_Scientist"),
    "platform": ("ml_platform", "ML_Platform"),
    "research": ("research_engineer", "Research_Engineer"),
}
VARIANT_ALIASES = {
    **VARIANTS,
    "applied_scientist": VARIANTS["applied"],
    "ml_platform": VARIANTS["platform"],
    "research_engineer": VARIANTS["research"],
}
REQUIRED_TOOLS = (
    "make",
    "xelatex",
    "latexmk",
    "pdfinfo",
    "pdftotext",
    "pdffonts",
    "pdftoppm",
    "qlmanage",
)
REQUIRED_COVER_FIELDS = (
    "Company",
    "RoleTitle",
    "HiringManager",
    "LetterDate",
    "OpeningReason",
    "EvidenceOne",
    "EvidenceTwo",
    "OrganisationFit",
    "ClosingParagraph",
)
PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|TBD|PLACEHOLDER)\b|\[\[|<\s*(?:company|role|manager|date|write)",
    re.IGNORECASE,
)
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SOURCE_DATE_EPOCH = "1704067200"
LATEX_AUXILIARY_GLOBS = (
    "*.acn",
    "*.acr",
    "*.alg",
    "*.aux",
    "*.bbl",
    "*.bcf",
    "*.blg",
    "*.brf",
    "*.dvi",
    "*.fdb_latexmk",
    "*.fls",
    "*.glg",
    "*.glo",
    "*.gls",
    "*.idx",
    "*.ilg",
    "*.ind",
    "*.ist",
    "*.loa",
    "*.lof",
    "*.lol",
    "*.lot",
    "*.log",
    "*.maf",
    "*.mtc",
    "*.mtc0",
    "*.nav",
    "*.nlo",
    "*.nls",
    "*.out",
    "*.pdfsync",
    "*.run.xml",
    "*.snm",
    "*.spl",
    "*.synctex",
    "*.synctex.gz",
    "*.toc",
    "*.upa",
    "*.upb",
    "*.vrb",
    "*.xdv",
    "*.xmpi",
    "texput.log",
)
LATEX_AUXILIARY_DIRECTORY_GLOBS = ("_minted-*", "_markdown_*")
CLEAN_PROTECTED_DIRECTORIES = frozenset({".git", ".vendor"})


class CliError(RuntimeError):
    """An expected, user-facing command failure."""


def repository_root() -> Path:
    override = os.environ.get("CV_REPO_ROOT")
    return Path(override).expanduser().resolve() if override else Path(__file__).resolve().parents[1]


def _slug_title(slug: str) -> str:
    return slug.replace("-", "_")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in value)


class CVApplication:
    """Stateful command dispatcher, with injectable streams for unit tests."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        self.root = (root or repository_root()).resolve()
        self.out = stdout or sys.stdout
        self.err = stderr or sys.stderr
        self.input = input_fn
        self.parser, self.subparsers = self._make_parser()

    def _make_parser(self) -> tuple[argparse.ArgumentParser, argparse._SubParsersAction]:
        parser = argparse.ArgumentParser(
            prog="cv",
            description="Build, inspect, and tailor Levon Rush's CV documents locally.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=(
                "Examples:\n"
                "  cv setup\n"
                "  cv build platform\n"
                "  cv build cv --no-logos\n"
                "  cv new cover microsoft-principal-applied-scientist\n"
                "  cv view cover microsoft-principal-applied-scientist"
            ),
        )
        subs = parser.add_subparsers(dest="command", metavar="COMMAND")

        setup = subs.add_parser("setup", help="install the command and fetch logo assets")
        setup.add_argument("--verbose", action="store_true")

        subs.add_parser("doctor", help="check the local document toolchain")

        build = subs.add_parser("build", help="build CV and cover-letter PDFs")
        build.add_argument(
            "target",
            nargs="?",
            default="all",
            choices=("all", "cv", "applied", "platform", "research", "cover"),
        )
        build.add_argument("application", nargs="?", help="cover-letter application slug")
        build.add_argument("--no-logos", action="store_true", help="produce text-only CV PDFs")
        build.add_argument("--verbose", action="store_true", help="show complete Make/LaTeX output")

        view = subs.add_parser("view", help="build if stale and open a PDF")
        view.add_argument(
            "target", nargs="?", default="applied",
            choices=("applied", "platform", "research", "cover", "all"),
        )
        view.add_argument("application", nargs="?", help="cover-letter application slug")
        view.add_argument("--verbose", action="store_true")

        new = subs.add_parser("new", help="create a tailored document")
        new.add_argument("kind", choices=("cover",))
        new.add_argument("slug")

        edit = subs.add_parser("edit", help="open a variant or application source in an editor")
        edit.add_argument("target")
        edit.add_argument("name", nargs="?")

        subs.add_parser("list", help="list variants, applications, outputs, and freshness")
        check = subs.add_parser("check", help="run CLI, PDF, ATS, and reproducibility checks")
        check.add_argument("--verbose", action="store_true")
        clean = subs.add_parser(
            "clean",
            help="remove build output and stray LaTeX files, preserving downloaded assets",
        )
        clean.add_argument("--verbose", action="store_true")

        assets = subs.add_parser("assets", help="inspect or fetch official logo assets")
        assets.add_argument("action", choices=("status", "fetch"))
        assets.add_argument("--force", action="store_true", help="download even verified assets")
        assets.add_argument("--verbose", action="store_true")

        help_parser = subs.add_parser("help", help="show general or command-specific help")
        help_parser.add_argument("topic", nargs="?", choices=tuple(subs.choices))
        return parser, subs

    def run(self, argv: Sequence[str] | None = None) -> int:
        args = self.parser.parse_args(list(argv) if argv is not None else None)
        if not args.command:
            self.parser.print_help(self.out)
            return 0
        handlers = {
            "setup": self.cmd_setup,
            "doctor": self.cmd_doctor,
            "build": self.cmd_build,
            "view": self.cmd_view,
            "new": self.cmd_new,
            "edit": self.cmd_edit,
            "list": self.cmd_list,
            "check": self.cmd_check,
            "clean": self.cmd_clean,
            "assets": self.cmd_assets,
            "help": self.cmd_help,
        }
        return handlers[args.command](args)

    def _say(self, message: str = "") -> None:
        print(message, file=self.out)

    def _warn(self, message: str) -> None:
        print(message, file=self.err)

    def _run_make(self, arguments: Sequence[str], *, verbose: bool = False) -> None:
        make = shutil.which("make")
        if not make:
            raise CliError("`make` is not installed. Run `cv doctor` for the full toolchain report.")
        command = [make, *arguments]
        if verbose and not any(item.startswith("VERBOSE=") for item in arguments):
            command.append("VERBOSE=1")
        environment = os.environ.copy()
        environment.setdefault("SOURCE_DATE_EPOCH", SOURCE_DATE_EPOCH)
        environment.setdefault("TZ", "UTC")
        if verbose:
            result = subprocess.run(command, cwd=self.root, env=environment, check=False)
            output = ""
        else:
            result = subprocess.run(
                command,
                cwd=self.root,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = result.stdout or ""
        if result.returncode:
            if output:
                lines = output.rstrip().splitlines()
                self._warn("\n".join(lines[-30:]))
            logs = sorted(self.root.glob("build/**/*.log"), key=lambda item: item.stat().st_mtime)
            suffix = f" Latest log: {logs[-1]}" if logs else ""
            raise CliError(f"Build command failed (exit {result.returncode}).{suffix}")

    def _variant_output(self, alias: str, *, logos: bool = True) -> Path:
        _, label = VARIANT_ALIASES[alias]
        no_logo = "_No_Logos" if not logos else ""
        return self.root / "build" / f"Levon_Rush_CV_{label}{no_logo}.pdf"

    def _cover_output(self, slug: str) -> Path:
        label = "Template" if slug == "template" else _slug_title(slug)
        return self.root / "build" / f"Levon_Rush_Cover_Letter_{label}.pdf"

    def _application_path(self, slug: str) -> Path:
        return self.root / "applications" / f"{slug}.tex"

    def _common_source_files(self) -> list[Path]:
        files: list[Path] = []
        for name in ("Makefile", "latexmkrc"):
            candidate = self.root / name
            if candidate.is_file():
                files.append(candidate)
        for directory in ("assets/fonts",):
            base = self.root / directory
            if base.is_dir():
                files.extend(item for item in base.rglob("*") if item.is_file())
        for name in ("src/design_system.tex", "src/commands.tex"):
            candidate = self.root / name
            if candidate.is_file():
                files.append(candidate)
        return files

    def _cv_source_files(self, alias: str, *, logos: bool = True) -> list[Path]:
        files = self._common_source_files()
        entry = self.root / "src" / "cv.tex"
        if entry.is_file():
            files.append(entry)
        content = self.root / "src" / "content"
        if content.is_dir():
            files.extend(item for item in content.rglob("*.tex") if item.is_file())
        variant = self.root / "variants" / f"{VARIANT_ALIASES[alias][0]}.tex"
        if variant.is_file():
            files.append(variant)
        if logos:
            logo_directory = self.root / ".vendor" / "logos"
            if logo_directory.is_dir():
                files.extend(item for item in logo_directory.iterdir() if item.is_file())
        return files

    def _cover_source_files(self, slug: str) -> list[Path]:
        files = self._common_source_files()
        entry = self.root / "src" / "cover_letter.tex"
        if entry.is_file():
            files.append(entry)
        snippets = self.root / "src" / "snippets" / "cover_letter"
        if snippets.is_dir():
            files.extend(item for item in snippets.rglob("*.tex") if item.is_file())
        application = self._application_path(slug)
        if application.is_file():
            files.append(application)
        return files

    def _freshness(self, output: Path, sources: Iterable[Path]) -> str:
        if not output.is_file():
            return "missing"
        latest_source = max((item.stat().st_mtime for item in sources), default=0.0)
        return "current" if output.stat().st_mtime >= latest_source else "stale"

    def _validate_slug(self, slug: str) -> None:
        if not SLUG_RE.fullmatch(slug):
            raise CliError(
                "Application slugs use lowercase letters, digits, and single hyphens "
                "(for example: microsoft-principal-scientist)."
            )

    def _validate_cover(self, slug: str) -> None:
        if slug == "template":
            return
        self._validate_slug(slug)
        path = self._application_path(slug)
        if not path.is_file():
            raise CliError(f"Cover application does not exist: {path}. Create it with `cv new cover {slug}`.")
        content = path.read_text(encoding="utf-8")
        validation_content = re.sub(r"(?m)(?<!\\)%.*$", " ", content)
        if PLACEHOLDER_RE.search(validation_content):
            raise CliError(f"{path} still contains TODO or placeholder text; finish it before building.")
        for field in REQUIRED_COVER_FIELDS:
            match = re.search(
                rf"\\(?:newcommand|renewcommand)\s*\{{\\{field}\}}\s*\{{([^}}]*)\}}",
                validation_content,
                re.DOTALL,
            )
            if not match or not match.group(1).strip():
                raise CliError(f"{path} must define a non-empty \\{field} field.")
        plain = validation_content
        plain = re.sub(r"\\[A-Za-z@]+\*?", " ", plain)
        plain = re.sub(r"[^A-Za-z0-9'’-]+", " ", plain)
        if len(plain.split()) > 650:
            raise CliError(f"{path} exceeds the 650-word application-source budget.")

    def cmd_build(self, args: argparse.Namespace) -> int:
        target, application = args.target, args.application
        if target != "cover" and application:
            raise CliError("An application slug can only follow `cv build cover`.")
        if args.no_logos and target not in ("cv", "applied", "platform", "research"):
            raise CliError("`--no-logos` is available only for CV builds.")
        if not args.no_logos and target in ("all", "cv", "applied", "platform", "research"):
            self._require_logo_assets()
        logos = "0" if args.no_logos else "1"
        if target == "all":
            make_args = ["all"]
            label = "all CVs and the cover-letter template"
        elif target == "cv":
            make_args = ["cvs", f"LOGOS={logos}"]
            label = "all text-only CVs" if args.no_logos else "all CVs"
        elif target in VARIANTS:
            variant = VARIANTS[target][0]
            make_args = ["cv", f"VARIANT={variant}", f"LOGOS={logos}"]
            label = f"the {target} CV"
        else:
            slug = application or "template"
            self._validate_cover(slug)
            make_args = ["cover", f"APP={slug}"]
            label = "the cover-letter template" if slug == "template" else f"cover letter '{slug}'"
        self._say(f"Building {label} …")
        self._run_make(make_args, verbose=args.verbose)
        self._say("Build complete.")
        return 0

    def _open_path(self, path: Path) -> None:
        configured = None if path.is_dir() else os.environ.get("CV_PDF_VIEWER")
        if configured:
            command = [*shlex.split(configured), str(path)]
        elif shutil.which("open"):
            command = [shutil.which("open") or "open", str(path)]
        elif shutil.which("xdg-open"):
            command = [shutil.which("xdg-open") or "xdg-open", str(path)]
        else:
            raise CliError("No PDF viewer was found. Set CV_PDF_VIEWER to a viewer command.")
        try:
            subprocess.Popen(
                command,
                cwd=self.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise CliError(f"Could not open {path}: {error}") from error

    def cmd_view(self, args: argparse.Namespace) -> int:
        if args.target == "all":
            if args.application:
                raise CliError("`cv view all` does not take an application slug.")
            build = self.root / "build"
            build.mkdir(parents=True, exist_ok=True)
            self._open_path(build)
            return 0
        if args.target == "cover":
            slug = args.application or "template"
            self._validate_cover(slug)
            output = self._cover_output(slug)
            make_args = ["cover", f"APP={slug}"]
        else:
            if args.application:
                raise CliError("An application slug can only follow `cv view cover`.")
            output = self._variant_output(args.target)
            make_args = ["cv", f"VARIANT={VARIANTS[args.target][0]}", "LOGOS=1"]
        sources = (
            self._cover_source_files(slug)
            if args.target == "cover"
            else self._cv_source_files(args.target, logos=True)
        )
        if self._freshness(output, sources) != "current":
            self._say(f"{output.name} is missing or stale; rebuilding …")
            self._run_make(make_args, verbose=args.verbose)
        if not output.is_file():
            raise CliError(f"The build succeeded but did not create the expected PDF: {output}")
        self._say(f"Opening {output}")
        self._open_path(output)
        return 0

    def _prompt(self, label: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        try:
            value = self.input(f"{label}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt) as error:
            raise CliError("Cover-letter creation was cancelled.") from error
        return value or (default or "")

    def cmd_new(self, args: argparse.Namespace) -> int:
        self._validate_slug(args.slug)
        path = self._application_path(args.slug)
        if path.exists():
            raise CliError(f"Refusing to overwrite existing application: {path}")
        company = self._prompt("Company")
        role = self._prompt("Role title")
        if not company or not role:
            raise CliError("Company and role title are required.")
        manager = self._prompt("Hiring manager", "Hiring Manager")
        today = dt.date.today()
        default_date = f"{today.day} {today.strftime('%B %Y')}"
        letter_date = self._prompt("Letter date", default_date)
        content = f"""% Generated by `cv new cover {args.slug}`. Complete TODO fields before building.
\\newcommand{{\\Company}}{{{_tex_escape(company)}}}
\\newcommand{{\\RoleTitle}}{{{_tex_escape(role)}}}
\\newcommand{{\\HiringManager}}{{{_tex_escape(manager)}}}
\\newcommand{{\\LetterDate}}{{{_tex_escape(letter_date)}}}
\\newcommand{{\\OpeningReason}}{{TODO: Explain why this role and organisation are compelling.}}
\\newcommand{{\\EvidenceOne}}{{\\AppliedScienceEvidence}}
\\newcommand{{\\EvidenceTwo}}{{\\MLPlatformEvidence}}
\\newcommand{{\\EvidenceThree}}{{}}
\\newcommand{{\\OrganisationFit}}{{TODO: Connect relevant evidence to the organisation's needs.}}
\\newcommand{{\\ClosingParagraph}}{{I would welcome the opportunity to discuss how my experience could contribute to \\Company.}}
"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._say(f"Created {path}")
        self._say("Complete the TODO fields, then run:")
        self._say(f"  cv edit cover {args.slug}")
        self._say(f"  cv build cover {args.slug}")
        return 0

    def _edit_path(self, target: str, name: str | None) -> Path:
        if target == "cover":
            slug = name or "template"
            self._validate_slug(slug)
            return self._application_path(slug)
        if name:
            raise CliError("A second name is supported only by `cv edit cover <slug>`.")
        if target in VARIANT_ALIASES:
            return self.root / "variants" / f"{VARIANT_ALIASES[target][0]}.tex"
        aliases = {
            "content": self.root / "src" / "content",
            "design": self.root / "src" / "design_system.tex",
            "commands": self.root / "src" / "commands.tex",
            "template": self._application_path("template"),
        }
        if target in aliases:
            return aliases[target]
        direct = self._application_path(target)
        if direct.is_file() and SLUG_RE.fullmatch(target):
            return direct
        raise CliError(f"Unknown edit target: {target}. Try applied, platform, research, content, design, or cover <slug>.")

    def cmd_edit(self, args: argparse.Namespace) -> int:
        path = self._edit_path(args.target, args.name)
        if not path.exists():
            raise CliError(f"Edit target does not exist: {path}")
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
        if editor:
            command = [*shlex.split(editor), str(path)]
        elif shutil.which("open"):
            command = [shutil.which("open") or "open", "-t", str(path)]
        elif shutil.which("xdg-open"):
            command = [shutil.which("xdg-open") or "xdg-open", str(path)]
        else:
            raise CliError("No editor was found. Set the EDITOR environment variable.")
        result = subprocess.run(command, cwd=self.root, check=False)
        if result.returncode:
            raise CliError(f"Editor exited with status {result.returncode}.")
        return 0

    def cmd_list(self, _args: argparse.Namespace) -> int:
        self._say("CV variants:")
        for alias in VARIANTS:
            output = self._variant_output(alias)
            state = self._freshness(output, self._cv_source_files(alias, logos=True))
            self._say(f"  {alias:<16} {state:<7} {output}")
            text_output = self._variant_output(alias, logos=False)
            text_state = self._freshness(
                text_output, self._cv_source_files(alias, logos=False)
            )
            self._say(f"  {alias + ' (text)':<16} {text_state:<7} {text_output}")
        self._say("Cover letters:")
        applications = sorted((self.root / "applications").glob("*.tex")) if (self.root / "applications").is_dir() else []
        if not applications:
            self._say("  (none)")
        for source in applications:
            output = self._cover_output(source.stem)
            state = self._freshness(output, self._cover_source_files(source.stem))
            self._say(f"  {source.stem:<24} {state:<7} {output}")
        return 0

    def _load_logo_manifest(self) -> list[dict[str, object]]:
        path = self.root / "assets" / "logo_sources.json"
        if not path.is_file():
            raise CliError(f"Logo manifest is missing: {path}")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CliError(f"Could not read logo manifest {path}: {error}") from error
        logos = manifest.get("logos") if isinstance(manifest, dict) else None
        if not isinstance(logos, list) or not logos:
            raise CliError(f"Logo manifest has no `logos` entries: {path}")
        return logos

    def _logo_target(self, record: dict[str, object]) -> Path:
        filename = record.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise CliError("Every logo manifest entry requires a safe basename in `filename`.")
        return self.root / ".vendor" / "logos" / filename

    def _asset_state(self, record: dict[str, object]) -> tuple[str, Path]:
        target = self._logo_target(record)
        expected = str(record.get("sha256", "")).lower()
        if not target.is_file():
            return "missing", target
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            return "manifest checksum invalid", target
        return ("verified" if _sha256(target) == expected else "checksum mismatch"), target

    def _require_logo_assets(self) -> None:
        issues: list[str] = []
        for record in self._load_logo_manifest():
            state, _ = self._asset_state(record)
            if state != "verified":
                issues.append(f"{record.get('id', 'logo')}: {state}")
        if issues:
            raise CliError(
                "Official logo assets are not ready ("
                + "; ".join(issues)
                + "). Run `cv setup`, or build text-only PDFs with "
                "`cv build cv --no-logos`."
            )

    def _download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "cv-local-builder/1.0"})
        # Standalone python.org builds on macOS can have an empty OpenSSL CA
        # search path even though the operating system provides a maintained
        # certificate bundle. Prefer an explicit trusted bundle in that case;
        # never fall back to disabling certificate verification.
        verify_paths = ssl.get_default_verify_paths()
        ca_candidates = (
            os.environ.get("SSL_CERT_FILE"),
            verify_paths.cafile,
            "/etc/ssl/cert.pem",
            "/opt/homebrew/etc/ca-certificates/cert.pem",
        )
        ca_file = next(
            (candidate for candidate in ca_candidates if candidate and Path(candidate).is_file()),
            None,
        )
        context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, timeout=45, context=context) as response:
                return response.read()
        except (OSError, urllib.error.URLError) as error:
            raise CliError(f"Unable to download {url}: {error}") from error

    @staticmethod
    def _verify_bytes(data: bytes, expected: object, label: str) -> None:
        if expected:
            actual = hashlib.sha256(data).hexdigest()
            if actual != str(expected).lower():
                raise CliError(f"{label} checksum mismatch: expected {expected}, received {actual}.")

    def _extract_acquired_asset(self, record: dict[str, object], payload: bytes, workspace: Path) -> Path:
        self._verify_bytes(payload, record.get("download_sha256"), "Downloaded asset")
        member = record.get("archive_member")
        if member:
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    member_name = str(member)
                    if member_name not in archive.namelist():
                        raise CliError(f"Official archive does not contain expected member: {member_name}")
                    payload = archive.read(member_name)
            except zipfile.BadZipFile as error:
                raise CliError("Downloaded logo pack is not a valid ZIP archive.") from error
        encoding = str(
            record.get(
                "download_content_encoding",
                record.get("source_encoding", record.get("encoding", "")),
            )
        ).lower()
        transform = str(record.get("transform", "")).lower()
        if encoding == "gzip" or "gunzip" in transform or "decompress" in transform:
            try:
                payload = gzip.decompress(payload)
            except OSError as error:
                raise CliError("Downloaded source is not valid gzip data.") from error
        if record.get("source_sha256"):
            self._verify_bytes(payload, record.get("source_sha256"), "Extracted source asset")
        source_suffix = Path(str(member or record.get("download_url", "source"))).suffix or ".source"
        source = workspace / f"source{source_suffix}"
        source.write_bytes(payload)
        target_name = str(record.get("filename"))
        output = workspace / target_name
        if not transform or transform in ("none", "copy"):
            output.write_bytes(payload)
            return output
        if "sips" in transform:
            if not shutil.which("sips"):
                raise CliError("This official logo transform requires macOS `sips`.")
            result = subprocess.run(
                ["sips", "-s", "format", "pdf", str(source), "--out", str(output)],
                check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        elif "qlmanage" in transform:
            if not shutil.which("qlmanage"):
                raise CliError("This official SVG transform requires macOS `qlmanage`.")
            preview_dir = workspace / "preview"
            preview_dir.mkdir()
            result = subprocess.run(
                ["qlmanage", "-t", "-s", "1200", "-o", str(preview_dir), str(source)],
                check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            candidates = sorted(preview_dir.glob("*.png"))
            if result.returncode == 0 and candidates:
                shutil.copyfile(candidates[0], output)
        else:
            raise CliError(f"Unsupported acquisition transform in logo manifest: {record.get('transform')}")
        if result.returncode or not output.is_file():
            details = (result.stdout or "").strip()
            raise CliError(f"Logo conversion failed. {details}".strip())
        return output

    def _fetch_assets(self, *, force: bool = False, verbose: bool = False) -> int:
        del verbose  # Downloads intentionally emit only concise status lines.
        failures = 0
        for record in self._load_logo_manifest():
            logo_id = str(record.get("id", record.get("organisation", "logo")))
            state, target = self._asset_state(record)
            if state == "verified" and not force:
                self._say(f"[ok] {logo_id}: already verified")
                continue
            url = record.get("download_url")
            if not isinstance(url, str) or not url:
                self._warn(f"[failed] {logo_id}: manifest has no download URL")
                failures += 1
                continue
            try:
                payload = self._download(url)
                with tempfile.TemporaryDirectory(prefix="cv-logo-") as temporary:
                    acquired = self._extract_acquired_asset(record, payload, Path(temporary))
                    expected = record.get("sha256")
                    if expected:
                        actual = _sha256(acquired)
                        if actual != str(expected).lower():
                            raise CliError(
                                f"Final asset checksum mismatch: expected {expected}, received {actual}."
                            )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary_target = target.with_suffix(target.suffix + ".part")
                    shutil.copyfile(acquired, temporary_target)
                    os.replace(temporary_target, target)
                self._say(f"[ok] {logo_id}: fetched {target}")
            except CliError as error:
                self._warn(f"[failed] {logo_id}: {error}")
                failures += 1
        return failures

    def _assets_status(self, *, emit: bool = True) -> int:
        failures = 0
        for record in self._load_logo_manifest():
            state, target = self._asset_state(record)
            logo_id = str(record.get("id", record.get("organisation", "logo")))
            marker = "ok" if state == "verified" else "issue"
            if emit:
                self._say(f"[{marker}] {logo_id}: {state} — {target}")
            failures += state != "verified"
        return int(bool(failures))

    def cmd_assets(self, args: argparse.Namespace) -> int:
        if args.action == "status":
            return self._assets_status()
        return int(bool(self._fetch_assets(force=args.force, verbose=args.verbose)))

    def _font_check(self) -> tuple[bool, str]:
        directory = self.root / "assets" / "fonts" / "ibm-plex-sans"
        files = [item for item in directory.rglob("*") if item.suffix.lower() in (".otf", ".ttf")] if directory.is_dir() else []
        normalised = [re.sub(r"[^a-z]", "", item.stem.lower()) for item in files]
        requirements = ("regular", "italic", "semibold", "semibolditalic")
        missing = [weight for weight in requirements if not any(weight in stem for stem in normalised)]
        return (not missing, str(directory) if not missing else "missing " + ", ".join(missing))

    def _viewer(self) -> str | None:
        configured = os.environ.get("CV_PDF_VIEWER")
        if configured:
            return configured
        return shutil.which("open") or shutil.which("xdg-open")

    def _install_path(self) -> Path:
        configured = os.environ.get("CV_BIN_DIR")
        directory = Path(configured).expanduser() if configured else Path.home() / ".local" / "bin"
        return directory / "cv"

    def _doctor_checks(self) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, bool, str]] = []
        checks.append(("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0]))
        for tool in REQUIRED_TOOLS:
            location = shutil.which(tool)
            checks.append((tool, bool(location), location or "not found"))
        fonts_ok, fonts_detail = self._font_check()
        checks.append(("IBM Plex Sans fonts", fonts_ok, fonts_detail))
        install = self._install_path()
        expected = self.root / "cv"
        installed = install.is_symlink() and install.resolve() == expected.resolve()
        checks.append(("cv command symlink", installed, str(install)))
        try:
            logos_ok = self._assets_status(emit=False) == 0
            logo_detail = str(self.root / ".vendor" / "logos")
        except CliError as error:
            logos_ok, logo_detail = False, str(error)
        checks.append(("official logo assets", logos_ok, logo_detail))
        viewer = self._viewer()
        checks.append(("PDF viewer", bool(viewer), viewer or "set CV_PDF_VIEWER"))
        return checks

    def cmd_doctor(self, _args: argparse.Namespace) -> int:
        self._say("CV toolchain report")
        checks = self._doctor_checks()
        for label, okay, detail in checks:
            self._say(f"[{'ok' if okay else 'missing'}] {label}: {detail}")
        self._say("[notice] Confirm permission to use organisation logos before external distribution.")
        failures = sum(not okay for _, okay, _ in checks)
        self._say("Ready." if not failures else f"{failures} required check(s) need attention.")
        return int(bool(failures))

    def _install_command(self) -> None:
        source = self.root / "cv"
        if not source.is_file():
            raise CliError(f"Repository command is missing: {source}")
        source.chmod(source.stat().st_mode | 0o111)
        destination = self._install_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            if destination.resolve() == source.resolve():
                self._say(f"[ok] command already installed: {destination}")
                return
            destination.unlink()
        elif destination.exists():
            raise CliError(
                f"Refusing to replace non-symlink command at {destination}. Move it aside and rerun setup."
            )
        destination.symlink_to(source)
        self._say(f"[ok] installed command: {destination} -> {source}")

    def cmd_setup(self, args: argparse.Namespace) -> int:
        self._install_command()
        failures = self._fetch_assets(force=False, verbose=args.verbose)
        doctor_result = self.cmd_doctor(argparse.Namespace())
        if failures:
            self._warn("Some official assets could not be fetched. Existing verified assets were preserved; retry `cv assets fetch` when online.")
        return int(bool(failures or doctor_result))

    def cmd_check(self, args: argparse.Namespace) -> int:
        self._say("Running document and CLI checks …")
        if self._assets_status():
            raise CliError("Official logo assets are missing or invalid. Run `cv assets fetch` and retry.")
        self._run_make(["check"], verbose=args.verbose)
        self._say("All checks passed.")
        return 0

    def _remove_stray_latex_files(self) -> int:
        """Remove editor/one-off TeX debris without touching Git or vendor assets."""
        files: set[Path] = set()
        directories: set[Path] = set()

        def protected(path: Path) -> bool:
            relative = path.relative_to(self.root)
            return bool(relative.parts and relative.parts[0] in CLEAN_PROTECTED_DIRECTORIES)

        for pattern in LATEX_AUXILIARY_GLOBS:
            files.update(
                path
                for path in self.root.rglob(pattern)
                if not protected(path) and (path.is_file() or path.is_symlink())
            )
        for pattern in LATEX_AUXILIARY_DIRECTORY_GLOBS:
            directories.update(
                path
                for path in self.root.rglob(pattern)
                if not protected(path) and path.is_dir()
            )

        removed = 0
        for path in sorted(files):
            path.unlink(missing_ok=True)
            removed += 1
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            if path.exists():
                shutil.rmtree(path)
                removed += 1
        return removed

    def cmd_clean(self, args: argparse.Namespace) -> int:
        self._say("Cleaning generated files …")
        self._run_make(["clean"], verbose=args.verbose)
        stray_count = self._remove_stray_latex_files()
        noun = "item" if stray_count == 1 else "items"
        self._say(
            f"Generated files removed, including {stray_count} stray LaTeX {noun}; "
            "downloaded logo assets were preserved."
        )
        return 0

    def cmd_help(self, args: argparse.Namespace) -> int:
        if args.topic:
            self.subparsers.choices[args.topic].print_help(self.out)
        else:
            self.parser.print_help(self.out)
        return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Run the command and convert expected failures into concise diagnostics."""
    application = CVApplication(root, stdout=stdout, stderr=stderr, input_fn=input_fn)
    try:
        return application.run(argv)
    except CliError as error:
        print(f"cv: {error}", file=stderr or sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cv: interrupted", file=stderr or sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
