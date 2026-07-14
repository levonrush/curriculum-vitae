from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.cv_cli import CVApplication, CliError, main


VALID_APPLICATION = r"""
\newcommand{\Company}{Example Corporation}
\newcommand{\RoleTitle}{Principal Applied Scientist}
\newcommand{\HiringManager}{Hiring Manager}
\newcommand{\LetterDate}{14 July 2026}
\newcommand{\OpeningReason}{The role joins applied research with operational delivery.}
\newcommand{\EvidenceOne}{\AppliedScienceEvidence}
\newcommand{\EvidenceTwo}{\MLPlatformEvidence}
\newcommand{\EvidenceThree}{}
\newcommand{\OrganisationFit}{My experience aligns with the organisation's needs.}
\newcommand{\ClosingParagraph}{I welcome the opportunity to discuss the role.}
"""


class CLITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        for directory in ("applications", "variants", "src/content", "assets/fonts/ibm-plex-sans"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        (self.root / "cv").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (self.root / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
        logo = self.root / ".vendor/logos/build-asset.pdf"
        logo.parent.mkdir(parents=True)
        logo.write_bytes(b"verified logo")
        manifest = {
            "schema_version": 1,
            "logos": [
                {
                    "id": "build_asset",
                    "filename": "build-asset.pdf",
                    "sha256": hashlib.sha256(logo.read_bytes()).hexdigest(),
                }
            ],
        }
        (self.root / "assets/logo_sources.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            isatty_fn=lambda: False,
            today_fn=lambda: dt.date(2026, 7, 14),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_app(self, *arguments: str) -> int:
        try:
            return self.app.run(arguments)
        except Exception as error:
            # Match the public main() conversion while retaining an injectable app.
            if isinstance(error, CliError):
                self.stderr.write(f"cv: {error}\n")
                return 2
            raise

    def interactive_app(self, *answers: str) -> tuple[CVApplication, list[str]]:
        responses = iter(answers)
        prompts: list[str] = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return next(responses)

        return (
            CVApplication(
                self.root,
                stdout=self.stdout,
                stderr=self.stderr,
                input_fn=answer,
                isatty_fn=lambda: True,
                today_fn=lambda: dt.date(2026, 7, 14),
            ),
            prompts,
        )

    def test_build_target_mapping(self) -> None:
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "applied"), 0)
            run_make.assert_called_once_with(
                ["cv", "VARIANT=applied_scientist"], verbose=False
            )
        self.assertIn("build/Levon_Rush_CV_Applied_Scientist.pdf", self.stdout.getvalue())
        self.assertNotIn(str(self.root), self.stdout.getvalue())

    def test_build_all_logo_cvs(self) -> None:
        with mock.patch.object(self.app, "_require_logo_assets") as require, mock.patch.object(
            self.app, "_run_make"
        ) as run_make:
            self.assertEqual(self.run_app("build", "all"), 0)
        require.assert_called_once_with()
        run_make.assert_called_once_with(["cvs"], verbose=False)

    def test_build_defaults_to_applied_logo_cv(self) -> None:
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build"), 0)
        run_make.assert_called_once_with(
            ["cv", "VARIANT=applied_scientist"], verbose=False
        )

    def test_friendly_variant_aliases_map_to_canonical_make_values(self) -> None:
        aliases = {
            "applied-scientist": "applied_scientist",
            "ml-platform": "ml_platform",
            "research-engineer": "research_engineer",
        }
        for alias, make_variant in aliases.items():
            with self.subTest(alias=alias), mock.patch.object(self.app, "_run_make") as run_make:
                self.assertEqual(self.run_app("build", alias), 0)
                run_make.assert_called_once_with(
                    ["cv", f"VARIANT={make_variant}"], verbose=False
                )

    def test_build_preflights_assets_and_maps_to_logo_output(self) -> None:
        with mock.patch.object(self.app, "_require_logo_assets") as require, mock.patch.object(
            self.app, "_run_make"
        ) as run_make:
            self.assertEqual(self.run_app("build", "platform"), 0)
        require.assert_called_once_with()
        run_make.assert_called_once_with(
            ["cv", "VARIANT=ml_platform"], verbose=False
        )
        self.assertIn("build/Levon_Rush_CV_ML_Platform.pdf", self.stdout.getvalue())

    def test_removed_logo_mode_flags_are_rejected_as_unrecognized(self) -> None:
        cases = (
            ("build", "applied", "--logos"),
            ("open", "applied", "--no-logos"),
            ("setup", "--logos"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with mock.patch.object(CVApplication, "_run_make") as run_make:
                    result = main(
                        arguments,
                        root=self.root,
                        stdout=io.StringIO(),
                        stderr=stderr,
                        isatty_fn=lambda: False,
                    )
                self.assertEqual(result, 2)
                run_make.assert_not_called()
                self.assertIn("unrecognized", stderr.getvalue().lower())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_missing_logo_assets_block_the_default_build(self) -> None:
        (self.root / ".vendor/logos/build-asset.pdf").unlink()
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "applied"), 2)
            run_make.assert_not_called()
        self.assertIn("cv setup", self.stderr.getvalue())

    def test_verbose_make_adds_backend_verbose_flag(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("tools.cv_cli.shutil.which", return_value="/usr/bin/make"), mock.patch(
            "tools.cv_cli.subprocess.run", return_value=completed
        ) as run:
            self.app._run_make(["all"], verbose=True)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/make", "all", "VERBOSE=1"])
        self.assertNotIn("stdout", run.call_args.kwargs)

    def test_missing_make_has_actionable_diagnostic(self) -> None:
        with mock.patch("tools.cv_cli.shutil.which", return_value=None):
            self.assertEqual(self.run_app("build", "applied"), 2)
        self.assertIn("cv setup", self.stderr.getvalue())

    def test_cover_build_validates_and_maps_application(self) -> None:
        (self.root / "applications/example-role.tex").write_text(
            VALID_APPLICATION, encoding="utf-8"
        )
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "cover", "example-role"), 0)
            run_make.assert_called_once_with(["cover", "APP=example-role"], verbose=False)
        self.assertEqual(
            self.app._cover_output("example-role").name,
            "Levon_Rush_Cover_Letter_example_role.pdf",
        )

    def test_multiline_legacy_cover_fields_still_validate_and_build(self) -> None:
        multiline = VALID_APPLICATION.replace(
            r"\newcommand{\OpeningReason}{The role joins applied research with operational delivery.}",
            """\\newcommand{\\OpeningReason}{
The role joins applied research
with operational delivery.
}""",
        ).replace(
            r"\newcommand{\OrganisationFit}{My experience aligns with the organisation's needs.}",
            """\\newcommand{\\OrganisationFit}{
My experience aligns with
the organisation's needs.
}""",
        )
        (self.root / "applications/multiline-role.tex").write_text(
            multiline, encoding="utf-8"
        )
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "cover", "multiline-role"), 0)
        run_make.assert_called_once_with(
            ["cover", "APP=multiline-role"], verbose=False
        )

    def test_cover_build_rejects_todo(self) -> None:
        path = self.root / "applications/example-role.tex"
        path.write_text(VALID_APPLICATION.replace("The role joins", "TODO: The role joins"), encoding="utf-8")
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "cover", "example-role"), 2)
            run_make.assert_not_called()
        self.assertIn("placeholder", self.stderr.getvalue())

    def test_cover_validation_ignores_todo_guidance_in_comments(self) -> None:
        (self.root / "applications/example-role.tex").write_text(
            "% Complete the TODO guidance before sending.\n" + VALID_APPLICATION,
            encoding="utf-8",
        )
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "cover", "example-role"), 0)
        run_make.assert_called_once_with(["cover", "APP=example-role"], verbose=False)

    def test_new_cover_collects_fields_and_refuses_overwrite(self) -> None:
        answers = iter(("Acme & Co", "Research_Engineer", "", ""))
        app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            input_fn=lambda _prompt: next(answers),
        )
        self.assertEqual(app.run(("new", "cover", "acme-research")), 0)
        content = (self.root / "applications/acme-research.tex").read_text(encoding="utf-8")
        self.assertIn(r"\newcommand{\Company}{Acme \& Co}", content)
        self.assertIn(r"\newcommand{\RoleTitle}{Research\_Engineer}", content)
        self.assertIn(r"\newcommand{\HiringManager}{Hiring Manager}", content)
        self.assertIn("TODO", content)
        with self.assertRaises(CliError):
            app.run(("new", "cover", "acme-research"))

    def test_new_cover_rejects_unsafe_slug_before_prompting(self) -> None:
        prompt = mock.Mock()
        app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            input_fn=prompt,
            isatty_fn=lambda: True,
        )
        with self.assertRaises(CliError):
            app.run(("new", "cover", "../unsafe"))
        prompt.assert_not_called()

    def test_cover_wizard_creates_escaped_application_with_ordered_evidence(self) -> None:
        app, _prompts = self.interactive_app(
            "",  # create the first tailored letter
            "Acme & Co",
            "R&D_Lead",
            "",
            "",
            "I want 50% ownership & impact.",
            "I connect research #1 to reliable delivery.",
            "",
            "3 1",
            "n",
        )
        with mock.patch.object(app, "_run_make") as run_make, mock.patch.object(
            app, "_open_path"
        ) as open_path:
            self.assertEqual(app.run(("cover",)), 0)

        path = self.root / "applications/acme-co-r-d-lead.tex"
        content = path.read_text(encoding="utf-8")
        self.assertIn(r"\newcommand{\Company}{Acme \& Co}", content)
        self.assertIn(r"\newcommand{\RoleTitle}{R\&D\_Lead}", content)
        self.assertIn(r"\newcommand{\LetterDate}{14 July 2026}", content)
        self.assertIn(r"\newcommand{\OpeningReason}{I want 50\% ownership \& impact.}", content)
        self.assertIn(r"\newcommand{\EvidenceOne}{\ResearchEngineeringEvidence}", content)
        self.assertIn(r"\newcommand{\EvidenceTwo}{\AppliedScienceEvidence}", content)
        self.assertIn(r"\newcommand{\EvidenceThree}{}", content)
        self.assertNotIn("TODO", content)
        self.assertEqual(list((self.root / "applications").glob("*.tmp")), [])
        self.assertEqual(list((self.root / "applications").glob(".*.tmp")), [])
        run_make.assert_not_called()
        open_path.assert_not_called()

    def test_cover_wizard_reprompts_invalid_required_date_and_evidence_fields(self) -> None:
        app, prompts = self.interactive_app(
            "",
            "",  # invalid company
            "Acme",
            "Research Engineer",
            "",
            "tomorrowish",  # invalid date
            "",  # accept deterministic default after the explanation
            "",  # invalid opening reason
            "The research-to-production scope is compelling.",
            "I can connect uncertainty work to Acme's operational needs.",
            "",
            "1 1",  # duplicate
            "1",  # too few
            "2 4",
            "n",
        )
        self.assertEqual(app.run(("cover",)), 0)
        content = (self.root / "applications/acme-research-engineer.tex").read_text(
            encoding="utf-8"
        )
        self.assertIn(r"\newcommand{\LetterDate}{14 July 2026}", content)
        self.assertIn(r"\newcommand{\EvidenceOne}{\MLPlatformEvidence}", content)
        self.assertIn(r"\newcommand{\EvidenceTwo}{\PrincipalTechnicalEvidence}", content)
        self.assertGreaterEqual(sum("Company" in prompt for prompt in prompts), 2)
        self.assertGreaterEqual(sum("Evidence" in prompt for prompt in prompts), 3)

    def test_cover_wizard_builds_opens_and_checks_one_page_by_default(self) -> None:
        app, _prompts = self.interactive_app(
            "",
            "Acme",
            "Research Engineer",
            "",
            "",
            "The role combines research and delivery.",
            "My experience matches Acme's needs.",
            "",
            "1 2 5",
            "",
        )
        output = self.root / "build/Levon_Rush_Cover_Letter_acme_research_engineer.pdf"

        def build(_args: object, *, verbose: bool = False) -> None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"pdf")

        with mock.patch.object(app, "_run_make", side_effect=build) as run_make, mock.patch.object(
            app, "_verify_cover_page_count"
        ) as verify_pages, mock.patch.object(app, "_open_path") as open_path:
            self.assertEqual(app.run(("cover",)), 0)
        run_make.assert_called_once_with(
            ["cover", "APP=acme-research-engineer"], verbose=False
        )
        verify_pages.assert_called_once_with(output)
        open_path.assert_called_once_with(output)

    def test_cover_wizard_page_count_failure_keeps_source_and_does_not_open(self) -> None:
        app, _prompts = self.interactive_app(
            "",
            "Acme",
            "Scientist",
            "",
            "",
            "The role is compelling.",
            "My evidence fits the organisation.",
            "",
            "1 3",
            "",
        )
        output = self.root / "build/Levon_Rush_Cover_Letter_acme_scientist.pdf"

        def build(_args: object, *, verbose: bool = False) -> None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"pdf")

        with mock.patch.object(app, "_run_make", side_effect=build), mock.patch.object(
            app,
            "_verify_cover_page_count",
            side_effect=CliError("The cover letter is 2 pages; it must fit on one page."),
        ), mock.patch.object(app, "_open_path") as open_path:
            with self.assertRaises(CliError) as context:
                app.run(("cover",))
        self.assertIn("2 pages", str(context.exception))
        self.assertTrue((self.root / "applications/acme-scientist.tex").is_file())
        open_path.assert_not_called()

    def test_cover_wizard_build_failure_keeps_completed_source(self) -> None:
        app, _prompts = self.interactive_app(
            "",
            "Acme",
            "Scientist",
            "",
            "",
            "The role is compelling.",
            "My evidence fits the organisation.",
            "",
            "1 3",
            "",
        )
        with mock.patch.object(
            app, "_run_make", side_effect=CliError("Build command failed (exit 1).")
        ), mock.patch.object(app, "_open_path") as open_path:
            with self.assertRaises(CliError):
                app.run(("cover",))
        self.assertTrue((self.root / "applications/acme-scientist.tex").is_file())
        open_path.assert_not_called()

    def test_cover_wizard_cancel_before_write_leaves_no_partial_application(self) -> None:
        answers = iter(("", "Acme"))

        def cancel(prompt: str) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise KeyboardInterrupt from None

        stderr = io.StringIO()
        result = main(
            ("cover",),
            root=self.root,
            stdout=io.StringIO(),
            stderr=stderr,
            input_fn=cancel,
            isatty_fn=lambda: True,
            today_fn=lambda: dt.date(2026, 7, 14),
        )
        self.assertEqual(result, 130)
        self.assertEqual(list((self.root / "applications").glob("*.tex")), [])

    def test_cover_wizard_collision_cancel_preserves_existing_application(self) -> None:
        path = self.root / "applications/acme-research-engineer.tex"
        original = VALID_APPLICATION.encode()
        path.write_bytes(original)
        app, _prompts = self.interactive_app(
            "n",
            "Acme",
            "Research Engineer",
            "b",
        )
        with mock.patch.object(app, "_run_make") as run_make:
            self.assertEqual(app.run(("cover",)), 0)
        self.assertEqual(path.read_bytes(), original)
        run_make.assert_not_called()

    def test_cover_wizard_collision_can_rename_without_overwriting(self) -> None:
        original_path = self.root / "applications/acme-research-engineer.tex"
        original = VALID_APPLICATION.encode()
        original_path.write_bytes(original)
        app, _prompts = self.interactive_app(
            "n",
            "Acme",
            "Research Engineer",
            "3",
            "acme-research-engineer-2",
            "",
            "",
            "A second opening reason.",
            "A second organisation fit.",
            "",
            "2 5",
            "n",
        )
        self.assertEqual(app.run(("cover",)), 0)
        self.assertEqual(original_path.read_bytes(), original)
        self.assertTrue(
            (self.root / "applications/acme-research-engineer-2.tex").is_file()
        )

    def test_simple_existing_cover_can_be_reviewed_in_the_wizard(self) -> None:
        path = self.root / "applications/example-role.tex"
        path.write_text(VALID_APPLICATION, encoding="utf-8")
        app, _prompts = self.interactive_app(
            "2",  # review or update
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "n",
        )
        self.assertEqual(app.run(("cover", "example-role")), 0)
        rewritten = path.read_text(encoding="utf-8")
        self.assertIn(r"\newcommand{\Company}{Example Corporation}", rewritten)
        self.assertIn(r"\newcommand{\EvidenceOne}{\AppliedScienceEvidence}", rewritten)

    def test_cover_wizard_preserves_custom_tex_and_points_to_advanced_editor(self) -> None:
        path = self.root / "applications/custom.tex"
        original = b"\\input{some-custom-layout.tex}\n"
        path.write_bytes(original)
        app, _prompts = self.interactive_app("1")
        with self.assertRaises(CliError) as context:
            app.run(("cover", "custom"))
        self.assertIn("cv edit cover custom", str(context.exception))
        self.assertEqual(path.read_bytes(), original)

    def test_inline_custom_tex_is_never_rewritten_by_the_wizard(self) -> None:
        path = self.root / "applications/inline-custom.tex"
        original = VALID_APPLICATION.replace(
            "The role joins applied research with operational delivery.",
            r"\textbf{The role joins applied research with operational delivery.}",
        ).encode()
        path.write_bytes(original)
        app, _prompts = self.interactive_app("2")
        with self.assertRaises(CliError) as context:
            app.run(("cover", "inline-custom"))
        self.assertIn("custom TeX", str(context.exception))
        self.assertIn("cv edit cover inline-custom", str(context.exception))
        self.assertEqual(path.read_bytes(), original)

    def test_cover_save_oserror_is_a_concise_cli_failure_without_partial_file(self) -> None:
        answers = iter(
            (
                "",
                "Acme",
                "Scientist",
                "",
                "",
                "The role is compelling.",
                "My experience fits Acme's needs.",
                "",
                "1 2",
            )
        )
        stderr = io.StringIO()
        with mock.patch("tools.cv_cli.os.replace", side_effect=OSError("disk full")):
            result = main(
                ("cover",),
                root=self.root,
                stdout=io.StringIO(),
                stderr=stderr,
                input_fn=lambda _prompt: next(answers),
                isatty_fn=lambda: True,
                today_fn=lambda: dt.date(2026, 7, 14),
            )
        self.assertEqual(result, 2)
        diagnostic = stderr.getvalue()
        self.assertIn("Could not safely save", diagnostic)
        self.assertIn("disk full", diagnostic)
        self.assertNotIn("Traceback", diagnostic)
        self.assertFalse((self.root / "applications/acme-scientist.tex").exists())
        self.assertEqual(list((self.root / "applications").glob(".*.tmp")), [])

    def test_noninteractive_cover_wizard_refuses_to_prompt(self) -> None:
        prompt = mock.Mock(side_effect=AssertionError("must not prompt"))
        app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            input_fn=prompt,
            isatty_fn=lambda: False,
        )
        with self.assertRaises(CliError) as context:
            app.run(("cover",))
        self.assertIn("terminal", str(context.exception).lower())
        prompt.assert_not_called()

    def test_noninteractive_missing_cover_name_errors_without_starting_wizard(self) -> None:
        prompt = mock.Mock(side_effect=AssertionError("must not prompt"))
        app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            input_fn=prompt,
            isatty_fn=lambda: False,
        )
        with self.assertRaises(CliError) as context:
            app.run(("cover", "missing-role"))
        self.assertIn("missing-role", str(context.exception))
        prompt.assert_not_called()

    def test_view_rebuilds_missing_pdf_then_opens_it(self) -> None:
        output = self.root / "build/Levon_Rush_CV_Applied_Scientist.pdf"

        def build(_args: object, *, verbose: bool = False) -> None:
            self.assertFalse(verbose)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"pdf")

        with mock.patch.object(self.app, "_run_make", side_effect=build) as run_make, mock.patch.object(
            self.app, "_open_path"
        ) as open_path:
            self.assertEqual(self.run_app("view", "applied"), 0)
        run_make.assert_called_once_with(
            ["cv", "VARIANT=applied_scientist"], verbose=False
        )
        open_path.assert_called_once_with(output)

    def test_current_view_does_not_build(self) -> None:
        output = self.root / "build/Levon_Rush_CV_ML_Platform.pdf"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"pdf")
        timestamp = (self.root / "Makefile").stat().st_mtime + 10
        os.utime(output, (timestamp, timestamp))
        with mock.patch.object(self.app, "_run_make") as run_make, mock.patch.object(
            self.app, "_open_path"
        ):
            self.assertEqual(self.run_app("view", "platform"), 0)
        run_make.assert_not_called()

    def test_open_defaults_to_applied_logo_cv_when_missing(self) -> None:
        output = self.root / "build/Levon_Rush_CV_Applied_Scientist.pdf"

        def build(_args: object, *, verbose: bool = False) -> None:
            self.assertFalse(verbose)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"pdf")

        with mock.patch.object(self.app, "_run_make", side_effect=build) as run_make, mock.patch.object(
            self.app, "_open_path"
        ) as open_path:
            self.assertEqual(self.run_app("open"), 0)
        run_make.assert_called_once_with(
            ["cv", "VARIANT=applied_scientist"], verbose=False
        )
        open_path.assert_called_once_with(output)

    def test_open_friendly_alias_uses_corresponding_logo_pdf(self) -> None:
        output = self.root / "build/Levon_Rush_CV_Research_Engineer.pdf"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"pdf")
        os.utime(output, (2_000_000_000, 2_000_000_000))
        with mock.patch.object(self.app, "_run_make") as run_make, mock.patch.object(
            self.app, "_open_path"
        ) as open_path:
            self.assertEqual(self.run_app("open", "research-engineer"), 0)
        run_make.assert_not_called()
        open_path.assert_called_once_with(output)

    def test_open_builds_logo_output_and_preflights_assets(self) -> None:
        output = self.root / "build/Levon_Rush_CV_ML_Platform.pdf"

        def build(_args: object, *, verbose: bool = False) -> None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"pdf")

        with mock.patch.object(self.app, "_require_logo_assets") as require, mock.patch.object(
            self.app, "_run_make", side_effect=build
        ) as run_make, mock.patch.object(self.app, "_open_path") as open_path:
            self.assertEqual(self.run_app("open", "ml-platform"), 0)
        self.assertGreaterEqual(require.call_count, 1)
        for call in require.call_args_list:
            self.assertEqual(call, mock.call())
        run_make.assert_called_once_with(
            ["cv", "VARIANT=ml_platform"], verbose=False
        )
        open_path.assert_called_once_with(output)

    def test_fresh_open_still_verifies_assets_before_opening(self) -> None:
        output = self.root / "build/Levon_Rush_CV_Applied_Scientist.pdf"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"pdf")
        os.utime(output, (2_000_000_000, 2_000_000_000))
        with mock.patch.object(self.app, "_require_logo_assets") as require, mock.patch.object(
            self.app, "_run_make"
        ) as run_make, mock.patch.object(self.app, "_open_path") as open_path:
            self.assertEqual(self.run_app("open", "applied"), 0)
        require.assert_called_once_with()
        run_make.assert_not_called()
        open_path.assert_called_once_with(output)

    def test_open_folder_creates_and_opens_build_directory_without_building(self) -> None:
        with mock.patch.object(self.app, "_run_make") as run_make, mock.patch.object(
            self.app, "_open_path"
        ) as open_path:
            self.assertEqual(self.run_app("open", "folder"), 0)
        run_make.assert_not_called()
        self.assertTrue((self.root / "build").is_dir())
        open_path.assert_called_once_with(self.root / "build")

    def test_open_failure_reports_the_successful_relative_pdf_path(self) -> None:
        output = self.root / "build/Levon_Rush_CV_Applied_Scientist.pdf"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"pdf")
        os.utime(output, (2_000_000_000, 2_000_000_000))
        with mock.patch.object(
            self.app, "_open_path", side_effect=CliError("viewer failed")
        ):
            self.assertEqual(self.run_app("open"), 2)
        error = self.stderr.getvalue()
        self.assertIn("build/Levon_Rush_CV_Applied_Scientist.pdf", error)
        self.assertNotIn(str(self.root), error)

    def test_unrelated_cover_source_does_not_stale_cv(self) -> None:
        variant = self.root / "variants/applied_scientist.tex"
        variant.write_text("% applied\n", encoding="utf-8")
        output = self.root / "build/Levon_Rush_CV_Applied_Scientist.pdf"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"pdf")
        output_time = variant.stat().st_mtime + 10
        os.utime(output, (output_time, output_time))
        application = self.root / "applications/new-role.tex"
        application.write_text(VALID_APPLICATION, encoding="utf-8")
        os.utime(application, (output_time + 10, output_time + 10))
        self.assertEqual(
            self.app._freshness(output, self.app._cv_source_files("applied")),
            "current",
        )

    def test_clean_delegates_to_make(self) -> None:
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("clean"), 0)
        run_make.assert_called_once_with(["clean"], verbose=False)

    def test_check_verifies_assets_then_delegates_to_make(self) -> None:
        payload = b"official logo"
        self._write_manifest(payload)
        target = self.root / ".vendor/logos/example.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("check", "--verbose"), 0)
        run_make.assert_called_once_with(["check"], verbose=True)

    def test_status_uses_friendly_states_and_only_lists_logo_cv_outputs(self) -> None:
        for name in ("applied_scientist", "ml_platform", "research_engineer"):
            (self.root / f"variants/{name}.tex").write_text("% variant\n", encoding="utf-8")
        applied = self.root / "build/Levon_Rush_CV_Applied_Scientist.pdf"
        platform = self.root / "build/Levon_Rush_CV_ML_Platform.pdf"
        applied.parent.mkdir(parents=True)
        for output in (applied, platform):
            output.write_bytes(b"pdf")
        os.utime(applied, (2_000_000_000, 2_000_000_000))
        os.utime(platform, (1_000_000_000, 1_000_000_000))
        (self.root / "variants/ml_platform.tex").touch()
        (self.root / "applications/example-role.tex").write_text(
            VALID_APPLICATION, encoding="utf-8"
        )
        (self.root / "applications/template.tex").write_text(
            VALID_APPLICATION, encoding="utf-8"
        )

        self.assertEqual(self.run_app("status"), 0)

        text = self.stdout.getvalue().lower()
        self.assertIn("ready", text)
        self.assertIn("needs rebuilding", text)
        self.assertIn("not built", text)
        self.assertIn("example corporation", text)
        self.assertIn("levon_rush_cover_letter_example_role.pdf", text)
        self.assertNotIn("template", text)
        self.assertIn("levon_rush_cv_applied_scientist.pdf", text)
        self.assertNotIn(str(self.root).lower(), text)

    def test_status_all_includes_template_and_logo_cv_outputs(self) -> None:
        (self.root / "applications/template.tex").write_text(
            VALID_APPLICATION, encoding="utf-8"
        )
        output = self.root / "build/Levon_Rush_CV_Applied_Scientist.pdf"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"pdf")
        os.utime(output, (2_000_000_000, 2_000_000_000))

        self.assertEqual(self.run_app("status", "--all"), 0)

        text = self.stdout.getvalue().lower()
        self.assertIn("template", text)
        self.assertIn("levon_rush_cv_applied_scientist.pdf", text)

    def test_setup_always_fetches_and_verifies_logo_assets(self) -> None:
        with mock.patch.object(self.app, "_install_command"), mock.patch.object(
            self.app, "_fetch_assets", return_value=0
        ) as fetch_assets, mock.patch.object(self.app, "cmd_doctor", return_value=0):
            self.assertEqual(self.run_app("setup"), 0)
        fetch_assets.assert_called_once_with(force=False, verbose=False)

    def test_setup_reports_logo_fetch_failure_as_not_ready(self) -> None:
        with mock.patch.object(self.app, "_install_command"), mock.patch.object(
            self.app, "_fetch_assets", return_value=1
        ), mock.patch.object(self.app, "cmd_doctor", return_value=0):
            self.assertEqual(self.run_app("setup"), 1)
        diagnostic = self.stderr.getvalue().lower()
        self.assertIn("logo", diagnostic)
        self.assertIn("cv setup", diagnostic)

    def test_doctor_treats_pdfinfo_and_logo_assets_as_everyday_requirements(self) -> None:
        (self.root / ".vendor/logos/build-asset.pdf").unlink()
        with mock.patch("tools.cv_cli.shutil.which", return_value=None):
            self.assertEqual(self.run_app("doctor"), 1)
        report = self.stdout.getvalue().lower()
        self.assertIn("everyday use", report)
        self.assertIn("full document checks", report)
        everyday = report.split("full document checks", maxsplit=1)[0]
        self.assertIn("pdfinfo", everyday)
        self.assertIn("logo", everyday)

    def test_edit_variant_uses_editor(self) -> None:
        source = self.root / "variants/ml_platform.tex"
        source.write_text("% variant\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"EDITOR": "code --wait"}, clear=False), mock.patch(
            "tools.cv_cli.subprocess.run"
        ) as run:
            run.return_value.returncode = 0
            self.assertEqual(self.run_app("edit", "platform"), 0)
        run.assert_called_once_with(["code", "--wait", str(source)], cwd=self.root, check=False)

    def test_bare_non_tty_prints_concise_help_without_prompting(self) -> None:
        prompt = mock.Mock(side_effect=AssertionError("non-TTY invocation must not prompt"))
        output = io.StringIO()
        app = CVApplication(
            self.root,
            stdout=output,
            stderr=io.StringIO(),
            input_fn=prompt,
            isatty_fn=lambda: False,
            today_fn=lambda: dt.date(2026, 7, 14),
        )
        self.assertEqual(app.run(()), 0)
        help_text = output.getvalue()
        for command in ("open", "build", "cover", "status", "setup", "guide"):
            self.assertIn(command, help_text)
        self.assertNotIn("--logos", help_text)
        self.assertNotIn("--no-logos", help_text)
        prompt.assert_not_called()

    def test_bare_tty_default_menu_choice_opens_main_cv(self) -> None:
        output = self.root / "build/Levon_Rush_CV_Applied_Scientist.pdf"
        prompts: list[str] = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return ""

        def build(_args: object, *, verbose: bool = False) -> None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"pdf")

        app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            input_fn=answer,
            isatty_fn=lambda: True,
            today_fn=lambda: dt.date(2026, 7, 14),
        )
        with mock.patch.object(app, "_run_make", side_effect=build), mock.patch.object(
            app, "_open_path"
        ) as open_path:
            self.assertEqual(app.run(()), 0)
        self.assertIn("Levon", self.stdout.getvalue())
        self.assertIn("Open my main CV", self.stdout.getvalue())
        self.assertEqual(len(prompts), 1)
        open_path.assert_called_once_with(output)

    def test_home_menu_invalid_choice_reprompts_and_quit_has_no_side_effects(self) -> None:
        answers = iter(("not a choice", "q"))
        prompts: list[str] = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            input_fn=answer,
            isatty_fn=lambda: True,
            today_fn=lambda: dt.date(2026, 7, 14),
        )
        with mock.patch.object(app, "_run_make") as run_make, mock.patch.object(
            app, "_open_path"
        ) as open_path:
            self.assertEqual(app.run(()), 0)
        self.assertEqual(len(prompts), 2)
        run_make.assert_not_called()
        open_path.assert_not_called()

    def test_back_from_cover_menu_returns_to_home_menu(self) -> None:
        (self.root / "applications/example-role.tex").write_text(
            VALID_APPLICATION, encoding="utf-8"
        )
        app, prompts = self.interactive_app("3", "b", "q")
        with mock.patch.object(app, "_run_make") as run_make, mock.patch.object(
            app, "_open_path"
        ) as open_path:
            self.assertEqual(app.run(()), 0)
        self.assertEqual(self.stdout.getvalue().count("Levon’s documents"), 2)
        self.assertEqual(len(prompts), 3)
        run_make.assert_not_called()
        open_path.assert_not_called()

    def test_alternate_cv_menu_can_open_research_variant(self) -> None:
        output = self.root / "build/Levon_Rush_CV_Research_Engineer.pdf"
        answers = iter(("2", "3"))

        def build(_args: object, *, verbose: bool = False) -> None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"pdf")

        app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            input_fn=lambda _prompt: next(answers),
            isatty_fn=lambda: True,
        )
        with mock.patch.object(app, "_run_make", side_effect=build) as run_make, mock.patch.object(
            app, "_open_path"
        ) as open_path:
            self.assertEqual(app.run(()), 0)
        run_make.assert_called_once_with(
            ["cv", "VARIANT=research_engineer"], verbose=False
        )
        open_path.assert_called_once_with(output)

    def test_ctrl_c_from_menu_returns_130(self) -> None:
        stderr = io.StringIO()
        result = main(
            (),
            root=self.root,
            stdout=io.StringIO(),
            stderr=stderr,
            input_fn=mock.Mock(side_effect=KeyboardInterrupt),
            isatty_fn=lambda: True,
            today_fn=lambda: dt.date(2026, 7, 14),
        )
        self.assertEqual(result, 130)
        self.assertIn("interrupted", stderr.getvalue().lower())

    def test_guide_displays_the_guided_home_screen(self) -> None:
        app = CVApplication(
            self.root,
            stdout=self.stdout,
            stderr=self.stderr,
            input_fn=lambda _prompt: "q",
            isatty_fn=lambda: True,
        )
        self.assertEqual(app.run(("guide",)), 0)
        self.assertIn("Open my main CV", self.stdout.getvalue())

    def test_primary_help_hides_advanced_commands(self) -> None:
        self.assertEqual(self.run_app("help"), 0)
        text = self.stdout.getvalue()
        for command in ("open", "build", "cover", "status", "setup", "guide"):
            self.assertIn(command, text)
        for advanced in ("doctor", "clean", "assets"):
            self.assertNotIn(f"  {advanced}", text)

    def test_advanced_help_lists_compatibility_commands(self) -> None:
        self.assertEqual(self.run_app("help", "advanced"), 0)
        text = self.stdout.getvalue()
        for command in ("view", "new cover", "edit", "list", "doctor", "check", "clean", "assets"):
            self.assertIn(command, text)

    def test_typo_has_short_suggestion_and_no_argparse_usage_dump(self) -> None:
        stderr = io.StringIO()
        result = main(
            ("buid",),
            root=self.root,
            stdout=io.StringIO(),
            stderr=stderr,
            isatty_fn=lambda: False,
        )
        self.assertEqual(result, 2)
        diagnostic = stderr.getvalue()
        self.assertIn("build", diagnostic)
        self.assertNotIn("usage:", diagnostic.lower())
        self.assertNotIn("Traceback", diagnostic)

    def _write_manifest(self, payload: bytes, *, target_payload: bytes | None = None) -> None:
        target_payload = target_payload if target_payload is not None else payload
        source = self.root / "official-logo.pdf"
        source.write_bytes(payload)
        manifest = {
            "schema_version": 1,
            "logos": [
                {
                    "id": "example",
                    "filename": "example.pdf",
                    "download_url": source.as_uri(),
                    "download_sha256": hashlib.sha256(payload).hexdigest(),
                    "sha256": hashlib.sha256(target_payload).hexdigest(),
                    "transform": None,
                }
            ],
        }
        (self.root / "assets/logo_sources.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_assets_fetch_and_status(self) -> None:
        payload = b"official logo"
        self._write_manifest(payload)
        self.assertEqual(self.run_app("assets", "fetch"), 0)
        target = self.root / ".vendor/logos/example.pdf"
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(self.run_app("assets", "status"), 0)
        target.write_bytes(b"tampered")
        self.assertEqual(self.run_app("assets", "status"), 1)

    def test_assets_fetch_preserves_verified_file(self) -> None:
        payload = b"official logo"
        self._write_manifest(payload)
        target = self.root / ".vendor/logos/example.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        with mock.patch.object(self.app, "_download") as download:
            self.assertEqual(self.run_app("assets", "fetch"), 0)
        download.assert_not_called()

    def test_assets_fetch_rejects_checksum_mismatch(self) -> None:
        payload = b"official logo"
        self._write_manifest(payload, target_payload=b"different expected logo")
        self.assertEqual(self.run_app("assets", "fetch"), 1)
        self.assertFalse((self.root / ".vendor/logos/example.pdf").exists())
        self.assertIn("Final asset checksum mismatch", self.stderr.getvalue())

    def test_setup_refuses_unrelated_symlink(self) -> None:
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        old_target = self.root / "old-cv"
        old_target.write_text("old", encoding="utf-8")
        (bin_directory / "cv").symlink_to(old_target)
        with mock.patch.dict(os.environ, {"CV_BIN_DIR": str(bin_directory)}, clear=False):
            with self.assertRaises(CliError):
                self.app._install_command()
        self.assertEqual((bin_directory / "cv").resolve(), old_target.resolve())

    def test_default_setup_installs_command_and_fetches_assets(self) -> None:
        bin_directory = self.root / "bin"
        with mock.patch.dict(os.environ, {"CV_BIN_DIR": str(bin_directory)}, clear=False), mock.patch.object(
            self.app, "_fetch_assets", return_value=0
        ) as fetch_assets, mock.patch.object(self.app, "cmd_doctor", return_value=0):
            result = self.run_app("setup")
        self.assertEqual(result, 0)
        self.assertEqual((bin_directory / "cv").resolve(), (self.root / "cv").resolve())
        fetch_assets.assert_called_once_with(force=False, verbose=False)

    def test_setup_is_safe_to_rerun_when_its_symlink_is_already_installed(self) -> None:
        bin_directory = self.root / "bin"
        with mock.patch.dict(os.environ, {"CV_BIN_DIR": str(bin_directory)}, clear=False), mock.patch.object(
            self.app, "cmd_doctor", return_value=0
        ):
            self.assertEqual(self.run_app("setup"), 0)
            self.assertEqual(self.run_app("setup"), 0)
        self.assertEqual((bin_directory / "cv").resolve(), (self.root / "cv").resolve())

    def test_setup_reports_when_the_command_directory_is_not_on_path(self) -> None:
        bin_directory = self.root / "private-bin"
        with mock.patch.dict(
            os.environ,
            {"CV_BIN_DIR": str(bin_directory), "PATH": "/usr/bin:/bin"},
            clear=False,
        ):
            self.assertEqual(self.run_app("setup"), 1)
        report = self.stdout.getvalue()
        self.assertIn("PATH", report)
        self.assertIn(str(bin_directory), report)

    def test_setup_refuses_to_replace_regular_file(self) -> None:
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        (bin_directory / "cv").write_text("owned by someone else", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CV_BIN_DIR": str(bin_directory)}, clear=False):
            with self.assertRaises(CliError):
                self.app._install_command()

    def test_clean_preserves_vendor_assets(self) -> None:
        (self.root / "Makefile").write_text("clean:\n\t@rm -rf build\n", encoding="utf-8")
        (self.root / "build").mkdir()
        (self.root / "build/generated.pdf").write_bytes(b"pdf")
        (self.root / "document.aux").write_text("temporary", encoding="utf-8")
        (self.root / "variants/document.synctex.gz").write_bytes(b"temporary")
        minted = self.root / "src/_minted-document"
        minted.mkdir()
        (minted / "cache.pygtex").write_text("temporary", encoding="utf-8")
        logo = self.root / ".vendor/logos/keep.pdf"
        logo.parent.mkdir(parents=True, exist_ok=True)
        logo.write_bytes(b"logo")
        protected_aux = self.root / ".vendor/logos/keep.aux"
        protected_aux.write_text("vendor data", encoding="utf-8")
        self.assertEqual(self.run_app("clean"), 0)
        self.assertFalse((self.root / "build").exists())
        self.assertFalse((self.root / "document.aux").exists())
        self.assertFalse((self.root / "variants/document.synctex.gz").exists())
        self.assertFalse(minted.exists())
        self.assertEqual(logo.read_bytes(), b"logo")
        self.assertEqual(protected_aux.read_text(encoding="utf-8"), "vendor data")

    def test_public_main_turns_expected_failure_into_exit_two(self) -> None:
        stderr = io.StringIO()
        result = main(
            ("new", "cover", "BAD SLUG"), root=self.root, stdout=io.StringIO(), stderr=stderr
        )
        self.assertEqual(result, 2)
        self.assertIn("lowercase", stderr.getvalue())

    def test_executable_bare_non_tty_smoke(self) -> None:
        command = Path(__file__).resolve().parents[1] / "cv"
        environment = {**os.environ, "CV_REPO_ROOT": str(self.root)}
        result = subprocess.run(
            [sys.executable, str(command)],
            cwd=self.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cv open", result.stdout)

    def test_executable_typo_smoke(self) -> None:
        command = Path(__file__).resolve().parents[1] / "cv"
        environment = {**os.environ, "CV_REPO_ROOT": str(self.root)}
        result = subprocess.run(
            [sys.executable, str(command), "buid"],
            cwd=self.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("build", result.stderr)
        self.assertNotIn("usage:", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
