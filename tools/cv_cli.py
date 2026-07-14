#!/usr/bin/env python3
"""Dependency-free local command line interface for the CV repository."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
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
import unicodedata
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
    "applied-scientist": VARIANTS["applied"],
    "applied_scientist": VARIANTS["applied"],
    "ml-platform": VARIANTS["platform"],
    "ml_platform": VARIANTS["platform"],
    "research-engineer": VARIANTS["research"],
    "research_engineer": VARIANTS["research"],
}
VARIANT_DESCRIPTIONS = {
    "applied": "balanced; best general choice",
    "platform": "platforms, governed delivery, and enablement",
    "research": "research, PhD, and uncertainty",
}
CORE_TOOLS = (
    "make",
    "xelatex",
    "latexmk",
    "pdfinfo",
)
CHECK_TOOLS = (
    "pdftotext",
    "pdffonts",
    "pdftoppm",
)
LOGO_TOOLS = (
    "qlmanage",
)
EVIDENCE_OPTIONS = {
    "1": (
        "Applied science",
        r"\AppliedScienceEvidence",
        "forecasting, anomaly detection, explainability, and operational delivery",
    ),
    "2": (
        "ML platforms",
        r"\MLPlatformEvidence",
        "Azure ML, reusable pipelines, CI/CD, dbt, and MLflow",
    ),
    "3": (
        "Research engineering",
        r"\ResearchEngineeringEvidence",
        "PhD research, domain adaptation, uncertainty, and validation",
    ),
    "4": (
        "Technical leadership",
        r"\PrincipalTechnicalEvidence",
        "reusable practice, production separation, mentoring, and enablement",
    ),
    "5": (
        "Infrastructure and utilities",
        r"\InfrastructureUtilitiesEvidence",
        "reliability, water security, governance, and engineering decisions",
    ),
}
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


class UserCancelled(RuntimeError):
    """An interactive workflow ended normally without making a change."""


class FriendlyArgumentParser(argparse.ArgumentParser):
    """Argument parser that turns expected mistakes into short diagnostics."""

    def error(self, message: str) -> None:
        invalid = re.search(r"invalid choice: '([^']+)' \(choose from (.+)\)", message)
        if invalid:
            value = invalid.group(1)
            choices = [item.strip(" '`\"") for item in invalid.group(2).split(",")]
            suggestion = difflib.get_close_matches(value, choices, n=1, cutoff=0.6)
            hint = f" Did you mean `{suggestion[0]}`?" if suggestion else ""
            raise CliError(f"I don't recognise `{value}`.{hint} Run `cv --help` for the easy commands.")
        raise CliError(f"{message}. Run `cv --help` for examples.")


def repository_root() -> Path:
    override = os.environ.get("CV_REPO_ROOT")
    return Path(override).expanduser().resolve() if override else Path(__file__).resolve().parents[1]


def _slug_title(slug: str) -> str:
    return slug.replace("-", "_")


def _derive_slug(company: str, role: str) -> str:
    value = unicodedata.normalize("NFKD", f"{company}-{role}")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", value))


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
        isatty_fn: Callable[[], bool] | None = None,
        today_fn: Callable[[], dt.date] = dt.date.today,
    ) -> None:
        self.root = (root or repository_root()).resolve()
        self.out = stdout or sys.stdout
        self.err = stderr or sys.stderr
        self.input = input_fn
        self.isatty = isatty_fn or (
            lambda: bool(sys.stdin.isatty() and getattr(self.out, "isatty", lambda: False)())
        )
        self.today = today_fn
        self.parser, self.subparsers = self._make_parser()

    def _make_parser(self) -> tuple[argparse.ArgumentParser, argparse._SubParsersAction]:
        parser = FriendlyArgumentParser(
            prog="cv",
            description="Build and open Levon's CVs and cover letters.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=(
                "Run `cv` with no command for the guided menu.\n\n"
                "CV types:\n"
                "  applied    balanced applied-science CV; the default\n"
                "  platform   ML-platform and governed-delivery CV\n"
                "  research   research-engineering and PhD-focused CV\n\n"
                "Every CV build includes the configured organisation logos.\n\n"
                "Examples:\n"
                "  cv\n"
                "  cv open\n"
                "  cv open platform\n"
                "  cv build all\n"
                "  cv cover\n"
                "  cv status\n\n"
                "Run `cv help advanced` for maintenance and compatibility commands."
            ),
        )
        subs = parser.add_subparsers(dest="command", metavar="COMMAND")

        guide = subs.add_parser("guide", help="show the guided menu")
        del guide

        open_parser = subs.add_parser("open", help="build if needed, then open a CV")
        open_parser.add_argument(
            "target",
            nargs="?",
            default="applied",
            choices=(*VARIANT_ALIASES, "folder", "all", "cover"),
            metavar="{applied,platform,research,folder}",
            help="CV type, folder, or advanced cover target",
        )
        open_parser.add_argument("application", nargs="?", help=argparse.SUPPRESS)
        open_parser.add_argument("--verbose", action="store_true", help="show technical build output")

        build = subs.add_parser("build", help="build a CV without opening it")
        build.add_argument(
            "target",
            nargs="?",
            default="applied",
            choices=("all", "cv", *VARIANT_ALIASES, "cover"),
            metavar="{applied,platform,research,all}",
            help="CV type; `all` builds all three",
        )
        build.add_argument("application", nargs="?", help=argparse.SUPPRESS)
        build.add_argument("--verbose", action="store_true", help="show technical build output")

        cover = subs.add_parser("cover", help="create, edit, or open a cover letter")
        cover.add_argument("slug", nargs="?", help="existing cover-letter name")
        cover.add_argument("--verbose", action="store_true", help="show technical build output")

        status = subs.add_parser("status", help="show which PDFs are ready")
        status.add_argument("--all", action="store_true", help="include the cover-letter template")

        setup = subs.add_parser("setup", help="prepare or troubleshoot this computer")
        setup.add_argument("--force", action="store_true", help="replace a conflicting symlink")
        setup.add_argument("--verbose", action="store_true")

        subs.add_parser("doctor", help=argparse.SUPPRESS)

        view = subs.add_parser("view", help=argparse.SUPPRESS)
        view.add_argument(
            "target", nargs="?", default="applied",
            choices=(*VARIANT_ALIASES, "cover", "all", "folder"),
        )
        view.add_argument("application", nargs="?", help="cover-letter application slug")
        view.add_argument("--verbose", action="store_true")

        new = subs.add_parser("new", help=argparse.SUPPRESS)
        new.add_argument("kind", choices=("cover",))
        new.add_argument("slug")

        edit = subs.add_parser("edit", help=argparse.SUPPRESS)
        edit.add_argument("target")
        edit.add_argument("name", nargs="?")

        list_parser = subs.add_parser("list", help=argparse.SUPPRESS)
        list_parser.add_argument("--all", action="store_true")
        check = subs.add_parser("check", help=argparse.SUPPRESS)
        check.add_argument("--verbose", action="store_true")
        clean = subs.add_parser(
            "clean",
            help=argparse.SUPPRESS,
        )
        clean.add_argument("--verbose", action="store_true")

        assets = subs.add_parser("assets", help=argparse.SUPPRESS)
        assets.add_argument("action", choices=("status", "fetch"))
        assets.add_argument("--force", action="store_true", help="download even verified assets")
        assets.add_argument("--verbose", action="store_true")

        help_parser = subs.add_parser("help", help="show general or command-specific help")
        help_parser.add_argument("topic", nargs="?")
        everyday = {"guide", "open", "build", "cover", "status", "setup", "help"}
        subs._choices_actions = [
            action for action in subs._choices_actions if action.dest in everyday
        ]
        return parser, subs

    def run(self, argv: Sequence[str] | None = None) -> int:
        arguments = list(argv) if argv is not None else sys.argv[1:]
        if not arguments:
            if self.isatty():
                return self.cmd_guide(argparse.Namespace())
            self.parser.print_help(self.out)
            return 0
        args = self.parser.parse_args(arguments)
        if not args.command:
            self.parser.print_help(self.out)
            return 0
        handlers = {
            "guide": self.cmd_guide,
            "open": self.cmd_view,
            "cover": self.cmd_cover,
            "status": self.cmd_status,
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
        try:
            return handlers[args.command](args)
        except UserCancelled:
            self._say("Cancelled.")
            return 0

    def _say(self, message: str = "") -> None:
        print(message, file=self.out)

    def _warn(self, message: str) -> None:
        print(message, file=self.err)

    def _run_make(self, arguments: Sequence[str], *, verbose: bool = False) -> None:
        make = shutil.which("make")
        if not make:
            raise CliError("`make` is not installed. Run `cv setup` for the exact requirements.")
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

    def _variant_output(self, alias: str) -> Path:
        _, label = VARIANT_ALIASES[alias]
        return self.root / "build" / f"Levon_Rush_CV_{label}.pdf"

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

    def _cv_source_files(self, alias: str) -> list[Path]:
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
                "Cover-letter names use lowercase letters, digits, and single hyphens "
                "(for example: microsoft-principal-scientist)."
            )

    def _extract_cover_fields(self, content: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        command = re.compile(
            r"\\(?:newcommand|renewcommand)\s*\{\s*\\([A-Za-z]+)\s*\}\s*\{"
        )
        for match in command.finditer(content):
            start = match.end()
            index = start
            depth = 1
            while index < len(content) and depth:
                char = content[index]
                if char == "\\" and index + 1 < len(content) and content[index + 1] in "{}%":
                    index += 2
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        fields[match.group(1)] = content[start:index].strip()
                        break
                index += 1
        return fields

    def _validate_cover_content(self, content: str, label: str = "cover letter") -> dict[str, str]:
        validation_content = re.sub(r"(?m)(?<!\\)%.*$", " ", content)
        if PLACEHOLDER_RE.search(validation_content):
            raise CliError(f"{label} still contains unfinished placeholder text.")
        fields = self._extract_cover_fields(validation_content)
        for field in REQUIRED_COVER_FIELDS:
            if not fields.get(field, "").strip():
                friendly = re.sub(r"(?<!^)(?=[A-Z])", " ", field).lower()
                raise CliError(f"{label} still needs {friendly}.")
        plain = validation_content
        plain = re.sub(r"\\[A-Za-z@]+\*?", " ", plain)
        plain = re.sub(r"[^A-Za-z0-9'’-]+", " ", plain)
        if len(plain.split()) > 650:
            raise CliError(f"{label} exceeds the 650-word source budget; shorten the prose and try again.")
        return fields

    def _validate_cover(self, slug: str) -> dict[str, str]:
        if slug == "template":
            return {}
        self._validate_slug(slug)
        path = self._application_path(slug)
        if not path.is_file():
            raise CliError(f"No cover letter named `{slug}` exists. Run `cv cover` to create one.")
        return self._validate_cover_content(path.read_text(encoding="utf-8"), path.name)

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def _canonical_variant(self, alias: str) -> str:
        value = VARIANT_ALIASES[alias]
        return next(name for name, record in VARIANTS.items() if record == value)

    def _build_cv(
        self,
        alias: str,
        *,
        verbose: bool = False,
        verify_assets: bool = True,
    ) -> Path:
        canonical = self._canonical_variant(alias)
        if verify_assets:
            self._require_logo_assets()
        make_args = ["cv", f"VARIANT={VARIANTS[canonical][0]}"]
        self._say(f"Building the {canonical} CV …")
        self._run_make(make_args, verbose=verbose)
        output = self._variant_output(canonical)
        self._say(f"✓ Ready: {self._relative(output)}")
        return output

    def _build_all_cvs(self, *, verbose: bool = False) -> list[Path]:
        self._require_logo_assets()
        self._say("Building all three CVs …")
        self._run_make(["cvs"], verbose=verbose)
        outputs = [self._variant_output(alias) for alias in VARIANTS]
        for output in outputs:
            self._say(f"✓ Ready: {self._relative(output)}")
        return outputs

    def _build_cover(self, slug: str, *, verbose: bool = False) -> Path:
        self._validate_cover(slug)
        self._say(f"Building cover letter `{slug}` …")
        self._run_make(["cover", f"APP={slug}"], verbose=verbose)
        output = self._cover_output(slug)
        self._say(f"✓ Ready: {self._relative(output)}")
        return output

    def cmd_build(self, args: argparse.Namespace) -> int:
        target, application = args.target, args.application
        if target != "cover" and application:
            raise CliError("An application slug can only follow `cv build cover`.")
        if target == "cover":
            slug = application or "template"
            self._build_cover(slug, verbose=args.verbose)
        elif target in ("all", "cv"):
            self._build_all_cvs(verbose=args.verbose)
        else:
            self._build_cv(target, verbose=args.verbose)
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
        if args.target in ("all", "folder"):
            if args.application:
                raise CliError("Opening the PDF folder does not take another name.")
            build = self.root / "build"
            build.mkdir(parents=True, exist_ok=True)
            self._say(f"Opening {self._relative(build)}/")
            self._open_path(build)
            return 0
        if args.target == "cover":
            slug = args.application or "template"
            return self._open_cover(slug, verbose=args.verbose)
        else:
            if args.application:
                raise CliError("An application slug can only follow `cv view cover`.")
            return self._open_cv(args.target, verbose=args.verbose)

    def _open_cv(self, alias: str, *, verbose: bool = False) -> int:
        canonical = self._canonical_variant(alias)
        self._require_logo_assets()
        output = self._variant_output(canonical)
        sources = self._cv_source_files(canonical)
        if self._freshness(output, sources) != "current":
            self._build_cv(canonical, verbose=verbose, verify_assets=False)
        if not output.is_file():
            raise CliError(f"The build finished but did not create {self._relative(output)}.")
        self._say(f"Opening {self._relative(output)}")
        try:
            self._open_path(output)
        except CliError as error:
            raise CliError(f"{error} The PDF is ready at {self._relative(output)}.") from error
        return 0

    def _open_cover(
        self,
        slug: str,
        *,
        verbose: bool = False,
        verify_pages: bool = False,
    ) -> int:
        self._validate_cover(slug)
        output = self._cover_output(slug)
        if self._freshness(output, self._cover_source_files(slug)) != "current":
            self._build_cover(slug, verbose=verbose)
        if not output.is_file():
            raise CliError(f"The build finished but did not create {self._relative(output)}.")
        if verify_pages and slug != "template":
            self._verify_cover_page_count(output)
        self._say(f"Opening {self._relative(output)}")
        try:
            self._open_path(output)
        except CliError as error:
            raise CliError(f"{error} The PDF is ready at {self._relative(output)}.") from error
        return 0

    def _verify_cover_page_count(self, path: Path) -> None:
        pdfinfo = shutil.which("pdfinfo")
        if not pdfinfo:
            raise CliError("`pdfinfo` is needed to confirm that the cover letter is one page. Run `cv setup`.")
        result = subprocess.run(
            [pdfinfo, str(path)],
            cwd=self.root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        match = re.search(r"(?m)^Pages:\s+(\d+)\s*$", result.stdout or "")
        if result.returncode or not match:
            raise CliError(f"I built {self._relative(path)}, but could not verify its page count.")
        pages = int(match.group(1))
        if pages != 1:
            raise CliError(
                f"{self._relative(path)} is {pages} pages; a cover letter must fit on one page. "
                "Run `cv cover` to shorten it."
            )

    def _prompt(self, label: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        try:
            value = self.input(f"{label}{suffix}: ").strip()
        except EOFError as error:
            raise UserCancelled from error
        return value or (default or "")

    def _prompt_required(self, label: str, default: str | None = None) -> str:
        while True:
            value = self._prompt(label, default)
            if value:
                return value
            self._say(f"{label} cannot be empty. Enter it, or press Ctrl-C to cancel.")

    def _prompt_plain_required(self, label: str, default: str | None = None) -> str:
        while True:
            value = self._prompt_required(label, default)
            if not PLACEHOLDER_RE.search(value):
                return value
            self._say(f"{label} still looks unfinished. Replace TODO/TBD text before continuing.")
            default = None

    def _prompt_slug(self, label: str) -> str:
        while True:
            value = self._prompt_required(label)
            if SLUG_RE.fullmatch(value):
                return value
            self._say("Use lowercase letters, numbers, and single hyphens only.")

    def _prompt_date(self, default: str) -> str:
        while True:
            value = self._prompt("Letter date", default)
            for date_format in ("%d %B %Y", "%Y-%m-%d"):
                try:
                    dt.datetime.strptime(value, date_format)
                    return value
                except ValueError:
                    continue
            self._say("Use a date such as `14 July 2026` or `2026-07-14`.")

    def _confirm(self, label: str, *, default: bool = True) -> bool:
        suffix = "Y/n" if default else "y/N"
        while True:
            value = self._prompt(f"{label} [{suffix}]").lower()
            if not value:
                return default
            if value in ("y", "yes"):
                return True
            if value in ("n", "no", "b", "back"):
                return False
            self._say("Please answer yes or no.")

    def _choice(
        self,
        prompt: str,
        choices: dict[str, str],
        *,
        default: str | None = None,
    ) -> str:
        while True:
            value = self._prompt(prompt, default).lower()
            if value in choices:
                return choices[value]
            named = [result for result in dict.fromkeys(choices.values()) if result not in ("back", "quit")]
            match = [item for item in named if item.lower().startswith(value)] if value else []
            if len(match) == 1:
                return match[0]
            self._say("Choose one of: " + ", ".join(choices) + ".")

    def cmd_guide(self, _args: argparse.Namespace) -> int:
        if not self.isatty():
            raise CliError("`cv guide` needs an interactive terminal. Run `cv --help` here instead.")
        while True:
            self._say("Levon’s documents")
            self._say()
            self._say("  1  Open my main CV")
            self._say("  2  Open a different CV")
            self._say("  3  Work on a cover letter")
            self._say("  4  Open the PDF folder")
            self._say("  5  Setup or troubleshoot")
            self._say("  q  Quit")
            self._say()
            choice = self._choice(
                "Choose",
                {
                    "1": "main",
                    "2": "different",
                    "3": "cover",
                    "4": "folder",
                    "5": "setup",
                    "q": "quit",
                    "quit": "quit",
                },
                default="1",
            )
            if choice == "quit":
                return 0
            if choice == "main":
                return self._open_cv("applied")
            if choice == "different":
                self._say("Choose a CV:")
                for number, alias in enumerate(VARIANTS, start=1):
                    title = alias.replace("_", " ").title()
                    self._say(f"  {number}  {title} — {VARIANT_DESCRIPTIONS[alias]}")
                self._say("  b  Back")
                selected = self._choice(
                    "Choose",
                    {"1": "applied", "2": "platform", "3": "research", "b": "back"},
                    default="1",
                )
                if selected == "back":
                    continue
                return self._open_cv(selected)
            if choice == "cover":
                result = self.cmd_cover(
                    argparse.Namespace(slug=None, verbose=False, from_guide=True)
                )
                if result == -1:
                    continue
                return result
            if choice == "folder":
                return self.cmd_view(
                    argparse.Namespace(
                        target="folder",
                        application=None,
                        verbose=False,
                    )
                )
            return self.cmd_setup(
                argparse.Namespace(force=False, verbose=False)
            )

    @staticmethod
    def _tex_unescape(value: str) -> str:
        replacements = (
            (r"\textbackslash{}", "\\"),
            (r"\textasciitilde{}", "~"),
            (r"\textasciicircum{}", "^"),
            (r"\&", "&"),
            (r"\%", "%"),
            (r"\$", "$"),
            (r"\#", "#"),
            (r"\_", "_"),
            (r"\{", "{"),
            (r"\}", "}"),
        )
        for escaped, plain in replacements:
            value = value.replace(escaped, plain)
        return value

    def _tailored_applications(self) -> list[Path]:
        directory = self.root / "applications"
        if not directory.is_dir():
            return []
        return [path for path in sorted(directory.glob("*.tex")) if path.stem != "template"]

    def _cover_summary(self, path: Path) -> tuple[str, str]:
        fields = self._extract_cover_fields(path.read_text(encoding="utf-8"))
        company = self._tex_unescape(fields.get("Company", path.stem))
        role = self._tex_unescape(fields.get("RoleTitle", "cover letter"))
        return company, role

    def _friendly_state(self, state: str) -> str:
        return {"current": "ready", "stale": "needs rebuilding", "missing": "not built"}[state]

    def _simple_cover_fields(self, path: Path) -> dict[str, str] | None:
        content = path.read_text(encoding="utf-8")
        fields = self._extract_cover_fields(content)
        allowed = set(REQUIRED_COVER_FIELDS) | {"EvidenceThree"}
        command = re.compile(
            r"^\\(?:newcommand|renewcommand)\s*\{\\([A-Za-z]+)\}\s*\{.*\}\s*$"
        )
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("%"):
                continue
            match = command.match(stripped)
            if not match or match.group(1) not in allowed:
                return None
        if not set(REQUIRED_COVER_FIELDS).issubset(fields):
            return None
        evidence_macros = {record[1] for record in EVIDENCE_OPTIONS.values()}
        for name in ("EvidenceOne", "EvidenceTwo"):
            if fields[name] not in evidence_macros:
                return None
        if fields.get("EvidenceThree", "") not in evidence_macros | {""}:
            return None
        for name in (
            "Company",
            "RoleTitle",
            "HiringManager",
            "LetterDate",
            "OpeningReason",
            "OrganisationFit",
            "ClosingParagraph",
        ):
            value = fields[name]
            if name == "ClosingParagraph":
                value = value.replace(r"\Company", fields["Company"])
            if _tex_escape(self._tex_unescape(value)) != value:
                return None
        return fields

    def _prompt_evidence(self, defaults: Sequence[str] = ("1", "2")) -> list[str]:
        self._say("Which experience best supports this role?")
        self._say("Choose 2 or 3 numbers, in the order they should appear.")
        for number, (label, _macro, description) in EVIDENCE_OPTIONS.items():
            self._say(f"  {number}  {label} — {description}")
        default = ",".join(defaults)
        while True:
            raw = self._prompt("Evidence", default)
            values = [item for item in re.split(r"[\s,]+", raw) if item]
            if len(values) not in (2, 3):
                self._say("Choose exactly two or three evidence numbers.")
                continue
            if len(set(values)) != len(values):
                self._say("Choose each evidence theme only once.")
                continue
            invalid = [value for value in values if value not in EVIDENCE_OPTIONS]
            if invalid:
                self._say("Evidence choices must be numbers from 1 to 5.")
                continue
            return values

    def _render_cover(self, values: dict[str, str], evidence: Sequence[str]) -> str:
        macros = [EVIDENCE_OPTIONS[number][1] for number in evidence]
        macros.extend([""] * (3 - len(macros)))
        return f"""% Generated by `cv cover`. Use the guided command to review this letter.
\\newcommand{{\\Company}}{{{_tex_escape(values['Company'])}}}
\\newcommand{{\\RoleTitle}}{{{_tex_escape(values['RoleTitle'])}}}
\\newcommand{{\\HiringManager}}{{{_tex_escape(values['HiringManager'])}}}
\\newcommand{{\\LetterDate}}{{{_tex_escape(values['LetterDate'])}}}
\\newcommand{{\\OpeningReason}}{{{_tex_escape(values['OpeningReason'])}}}
\\newcommand{{\\EvidenceOne}}{{{macros[0]}}}
\\newcommand{{\\EvidenceTwo}}{{{macros[1]}}}
\\newcommand{{\\EvidenceThree}}{{{macros[2]}}}
\\newcommand{{\\OrganisationFit}}{{{_tex_escape(values['OrganisationFit'])}}}
\\newcommand{{\\ClosingParagraph}}{{{_tex_escape(values['ClosingParagraph'])}}}
"""

    def _write_cover_atomically(self, slug: str, content: str) -> Path:
        path = self._application_path(slug)
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{slug}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            self._validate_cover_content(temporary.read_text(encoding="utf-8"), path.name)
            os.replace(temporary, path)
            return path
        except OSError as error:
            raise CliError(f"Could not safely save {self._relative(path)}: {error}") from error
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _cover_wizard(
        self,
        *,
        slug: str | None = None,
        existing: dict[str, str] | None = None,
        verbose: bool = False,
    ) -> int:
        existing = existing or {}

        def previous(name: str) -> str | None:
            value = self._tex_unescape(existing.get(name, ""))
            return None if not value or PLACEHOLDER_RE.search(value) else value

        self._say("New cover letter" if not existing else "Review cover letter")
        company = self._prompt_plain_required("Company or organisation", previous("Company"))
        role = self._prompt_plain_required("Role title", previous("RoleTitle"))
        if slug is None:
            slug = _derive_slug(company, role)
            if not slug:
                slug = self._prompt_slug("Short file name")
        self._validate_slug(slug)

        path = self._application_path(slug)
        if path.exists() and not existing:
            old_company, old_role = self._cover_summary(path)
            self._say(f"A cover letter for {old_company} — {old_role} already exists.")
            self._say("  1  Open the existing letter")
            self._say("  2  Update the existing letter")
            self._say("  3  Create another with a different name")
            self._say("  b  Cancel")
            choice = self._choice(
                "Choose",
                {"1": "open", "2": "update", "3": "rename", "b": "back"},
                default="1",
            )
            if choice == "open":
                return self._resume_cover(slug, verbose=verbose)
            if choice == "update":
                fields = self._simple_cover_fields(path)
                if fields is None:
                    raise CliError(f"{path.name} has custom TeX. Use `cv edit cover {slug}` to preserve it.")
                return self._cover_wizard(slug=slug, existing=fields, verbose=verbose)
            if choice == "back":
                return 0
            while True:
                replacement = self._prompt_slug("Different short name")
                if not self._application_path(replacement).exists():
                    slug = replacement
                    break
                self._say(f"`{replacement}` already exists; choose another name.")

        manager = self._prompt("Hiring manager", previous("HiringManager") or "Hiring Manager")
        today = self.today()
        default_date = f"{today.day} {today.strftime('%B %Y')}"
        letter_date = self._prompt_date(previous("LetterDate") or default_date)
        opening = self._prompt_plain_required("Why this role and organisation", previous("OpeningReason"))
        fit = self._prompt_plain_required("Why you are a good fit", previous("OrganisationFit"))
        closing_previous = previous("ClosingParagraph")
        if closing_previous:
            closing_previous = closing_previous.replace(r"\Company", company)
        closing_default = closing_previous or (
            f"I would welcome the opportunity to discuss how my experience could contribute to {company}."
        )
        closing = self._prompt_plain_required("Closing", closing_default)

        reverse_evidence = {record[1]: number for number, record in EVIDENCE_OPTIONS.items()}
        evidence_defaults = [
            reverse_evidence[value]
            for name in ("EvidenceOne", "EvidenceTwo", "EvidenceThree")
            if (value := existing.get(name, "")) in reverse_evidence
        ] or ["1", "2"]
        evidence = self._prompt_evidence(evidence_defaults)
        while True:
            values = {
                "Company": company,
                "RoleTitle": role,
                "HiringManager": manager,
                "LetterDate": letter_date,
                "OpeningReason": opening,
                "OrganisationFit": fit,
                "ClosingParagraph": closing,
            }
            content = self._render_cover(values, evidence)
            try:
                self._validate_cover_content(content, "This cover letter")
                break
            except CliError as error:
                if "650-word" not in str(error):
                    raise
                self._say(str(error))
                opening = self._prompt_plain_required("Shorter reason for this role")
                fit = self._prompt_plain_required("Shorter explanation of your fit")
                closing = self._prompt_plain_required("Shorter closing", closing_default)
        saved = self._write_cover_atomically(slug, content)
        self._say(f"✓ Saved: {self._relative(saved)}")
        try:
            build_now = self._confirm("Build and open it now?", default=True)
        except UserCancelled:
            self._say(f"The completed source remains saved at {self._relative(saved)}.")
            return 0
        except KeyboardInterrupt:
            self._warn(f"The completed source remains saved at {self._relative(saved)}.")
            raise
        if build_now:
            return self._open_cover(slug, verbose=verbose, verify_pages=True)
        return 0

    def _resume_cover(
        self,
        slug: str,
        *,
        verbose: bool = False,
        back_code: int = 0,
    ) -> int:
        path = self._application_path(slug)
        if not path.is_file():
            if not self.isatty():
                raise CliError(
                    f"No cover letter named `{slug}` exists. Run `cv cover` in a terminal to create one."
                )
            return self._cover_wizard(slug=slug, verbose=verbose)
        fields = self._simple_cover_fields(path)
        try:
            valid = bool(self._validate_cover(slug) is not None)
        except CliError:
            valid = False
        if not self.isatty():
            if not valid:
                raise CliError(f"{path.name} is unfinished. Run `cv cover {slug}` in a terminal to finish it.")
            return self._open_cover(slug, verbose=verbose, verify_pages=True)
        company, role = self._cover_summary(path)
        self._say(f"{company} — {role}")
        if valid:
            self._say("  1  Build and open")
            self._say("  2  Review or update")
            self._say("  3  Edit the source (advanced)")
            self._say("  b  Back")
            choice = self._choice(
                "Choose",
                {"1": "open", "2": "update", "3": "edit", "b": "back"},
                default="1",
            )
        else:
            self._say("This letter is unfinished.")
            self._say("  1  Finish it")
            self._say("  2  Edit the source (advanced)")
            self._say("  b  Back")
            choice = self._choice(
                "Choose", {"1": "update", "2": "edit", "b": "back"}, default="1"
            )
        if choice == "open":
            return self._open_cover(slug, verbose=verbose, verify_pages=True)
        if choice == "update":
            if fields is None:
                raise CliError(f"{path.name} has custom TeX. Use `cv edit cover {slug}` to preserve it.")
            return self._cover_wizard(slug=slug, existing=fields, verbose=verbose)
        if choice == "edit":
            return self.cmd_edit(argparse.Namespace(target="cover", name=slug))
        return back_code

    def cmd_cover(self, args: argparse.Namespace) -> int:
        back_result = -1 if getattr(args, "from_guide", False) else 0
        if args.slug:
            if args.slug == "template":
                raise CliError("The template is hidden from the guided flow. Use `cv build cover template` if needed.")
            self._validate_slug(args.slug)
            return self._resume_cover(args.slug, verbose=args.verbose)
        if not self.isatty():
            raise CliError("`cv cover` needs an interactive terminal. Run it in Terminal to create a letter.")
        applications = self._tailored_applications()
        if not applications:
            self._say("You do not have any tailored cover letters yet.")
            if not self._confirm("Create one now?", default=True):
                return back_result
            return self._cover_wizard(verbose=args.verbose)
        while True:
            self._say("Cover letters")
            choices: dict[str, str] = {"n": "new", "b": "back"}
            for number, path in enumerate(applications, start=1):
                company, role = self._cover_summary(path)
                state = self._friendly_state(
                    self._freshness(
                        self._cover_output(path.stem), self._cover_source_files(path.stem)
                    )
                )
                self._say(f"  {number}  {company} — {role}  ({state})")
                choices[str(number)] = path.stem
            self._say("  n  Create a new cover letter")
            self._say("  b  Back")
            selected = self._choice("Choose", choices)
            if selected == "back":
                return back_result
            if selected == "new":
                return self._cover_wizard(verbose=args.verbose)
            result = self._resume_cover(selected, verbose=args.verbose, back_code=-1)
            if result != -1:
                return result

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
        today = self.today()
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

    def cmd_status(self, args: argparse.Namespace) -> int:
        show_all = bool(getattr(args, "all", False))
        self._say("CVs")
        try:
            assets_ready = self._assets_status(emit=False) == 0
        except CliError:
            assets_ready = False
        for alias in VARIANTS:
            output = self._variant_output(alias)
            state = self._freshness(output, self._cv_source_files(alias))
            friendly_state = self._friendly_state(state) if assets_ready else "logo assets missing"
            marker = " (default)" if alias == "applied" else ""
            self._say(
                f"  {alias}{marker:<10} {friendly_state:<19} {self._relative(output)}"
            )
        self._say("Cover letters")
        applications = self._tailored_applications()
        if show_all:
            template = self._application_path("template")
            if template.is_file():
                applications = [template, *applications]
        if not applications:
            self._say("  (none)")
        for source in applications:
            output = self._cover_output(source.stem)
            state = self._freshness(output, self._cover_source_files(source.stem))
            if source.stem == "template":
                label = "template"
            else:
                company, role = self._cover_summary(source)
                label = f"{company} — {role}"
            self._say(
                f"  {label:<32} {self._friendly_state(state):<17} {self._relative(output)}"
            )
        return 0

    def cmd_list(self, args: argparse.Namespace) -> int:
        return self.cmd_status(args)

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
                + "). Run `cv setup` and try again."
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

    def _path_contains(self, directory: Path) -> bool:
        entries = [Path(value).expanduser() for value in os.environ.get("PATH", "").split(os.pathsep) if value]
        return any(entry.resolve() == directory.expanduser().resolve() for entry in entries)

    def _doctor_groups(self) -> dict[str, list[tuple[str, bool, str]]]:
        required: list[tuple[str, bool, str]] = []
        required.append(("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0]))
        for tool in CORE_TOOLS:
            location = shutil.which(tool)
            required.append((tool, bool(location), location or "not found; see README > Toolchain"))
        for tool in LOGO_TOOLS:
            location = shutil.which(tool)
            required.append((tool, bool(location), location or "needed to prepare required logo assets"))
        fonts_ok, fonts_detail = self._font_check()
        required.append(("IBM Plex Sans fonts", fonts_ok, fonts_detail))
        try:
            logos_ok = self._assets_status(emit=False) == 0
            logo_detail = str(self.root / ".vendor" / "logos")
        except CliError as error:
            logos_ok, logo_detail = False, str(error)
        required.append(("organisation logo assets", logos_ok, logo_detail))
        install = self._install_path()
        expected = self.root / "cv"
        installed = install.is_symlink() and install.resolve() == expected.resolve()
        required.append(("cv command", installed, str(install)))
        on_path = self._path_contains(install.parent)
        required.append(
            (
                "command directory on PATH",
                on_path,
                str(install.parent) if on_path else f"add {install.parent} to PATH",
            )
        )
        viewer = self._viewer()
        required.append(("PDF viewer", bool(viewer), viewer or "set CV_PDF_VIEWER"))

        full_checks: list[tuple[str, bool, str]] = []
        for tool in CHECK_TOOLS:
            location = shutil.which(tool)
            full_checks.append((tool, bool(location), location or "not found; install Poppler for `cv check`"))

        return {"Everyday use": required, "Full document checks": full_checks}

    def _doctor_checks(self) -> list[tuple[str, bool, str]]:
        """Flattened compatibility view used by older tests and integrations."""
        return [check for group in self._doctor_groups().values() for check in group]

    def cmd_doctor(self, _args: argparse.Namespace) -> int:
        self._say("CV setup report")
        groups = self._doctor_groups()
        for heading, checks in groups.items():
            self._say(heading)
            for label, okay, detail in checks:
                marker = "ok" if okay else "missing"
                self._say(f"  [{marker}] {label}: {detail}")
        failures = sum(not okay for _, okay, _ in groups["Everyday use"])
        self._say("Ready for everyday use." if not failures else f"{failures} everyday setup item(s) need attention.")
        return int(bool(failures))

    def _install_command(self, *, force: bool = False) -> None:
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
            if not force:
                if not self.isatty() or not self._confirm(
                    f"{destination} points somewhere else. Replace that symlink?", default=False
                ):
                    raise CliError(
                        f"Refusing to replace the unrelated symlink at {destination}. "
                        "Move it aside or rerun `cv setup --force`."
                    )
            destination.unlink()
        elif destination.exists():
            raise CliError(
                f"Refusing to replace non-symlink command at {destination}. Move it aside and rerun setup."
            )
        destination.symlink_to(source)
        self._say(f"[ok] installed command: {destination} -> {source}")

    def cmd_setup(self, args: argparse.Namespace) -> int:
        self._install_command(force=bool(getattr(args, "force", False)))
        failures = self._fetch_assets(force=False, verbose=args.verbose)
        doctor_result = self.cmd_doctor(argparse.Namespace())
        if failures:
            self._warn(
                "Required organisation logos could not be prepared. Existing verified assets were "
                "preserved; retry `cv setup` when online."
            )
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
        if not args.topic:
            self.parser.print_help(self.out)
            return 0
        if args.topic == "advanced":
            self._say("Advanced and compatibility commands")
            self._say("  cv view ...          compatibility alias for `cv open`")
            self._say("  cv view cover NAME   build if needed and open a cover letter")
            self._say("  cv build cv          build all three CVs")
            self._say("  cv build cover NAME  build a cover letter or the explicit template")
            self._say("  cv new cover NAME    create the older editable TODO template")
            self._say("  cv edit ...          open source content in an editor")
            self._say("  cv list [--all]      compatibility alias for `cv status`")
            self._say("  cv doctor            detailed toolchain report")
            self._say("  cv assets status|fetch")
            self._say("  cv check [--verbose] run all PDF, ATS, and reproducibility checks")
            self._say("  cv clean             remove generated files")
            self._say()
            self._say("Run `cv help COMMAND` for command-specific usage.")
            return 0
        parser = self.subparsers.choices.get(args.topic)
        if not parser:
            suggestion = difflib.get_close_matches(args.topic, self.subparsers.choices, n=1, cutoff=0.6)
            hint = f" Did you mean `{suggestion[0]}`?" if suggestion else ""
            raise CliError(f"There is no help topic `{args.topic}`.{hint}")
        parser.print_help(self.out)
        return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    input_fn: Callable[[str], str] = input,
    isatty_fn: Callable[[], bool] | None = None,
    today_fn: Callable[[], dt.date] = dt.date.today,
) -> int:
    """Run the command and convert expected failures into concise diagnostics."""
    application = CVApplication(
        root,
        stdout=stdout,
        stderr=stderr,
        input_fn=input_fn,
        isatty_fn=isatty_fn,
        today_fn=today_fn,
    )
    try:
        return application.run(argv)
    except CliError as error:
        print(f"cv: {error}", file=stderr or sys.stderr)
        return 2
    except UserCancelled:
        print("Cancelled.", file=stdout or sys.stdout)
        return 0
    except KeyboardInterrupt:
        print("cv: interrupted", file=stderr or sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
