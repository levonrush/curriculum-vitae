from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
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
        self.app = CVApplication(self.root, stdout=self.stdout, stderr=self.stderr)

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

    def test_build_target_mapping(self) -> None:
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "applied"), 0)
            run_make.assert_called_once_with(
                ["cv", "VARIANT=applied_scientist", "LOGOS=1"], verbose=False
            )

    def test_build_all_text_only_cvs(self) -> None:
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "cv", "--no-logos"), 0)
            run_make.assert_called_once_with(["cvs", "LOGOS=0"], verbose=False)

    def test_logo_build_rejects_missing_assets_but_text_only_still_maps(self) -> None:
        (self.root / ".vendor/logos/build-asset.pdf").unlink()
        with mock.patch.object(self.app, "_run_make") as run_make:
            self.assertEqual(self.run_app("build", "applied"), 2)
            run_make.assert_not_called()
            self.assertEqual(self.run_app("build", "applied", "--no-logos"), 0)
            run_make.assert_called_once_with(
                ["cv", "VARIANT=applied_scientist", "LOGOS=0"], verbose=False
            )
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
        self.assertIn("cv doctor", self.stderr.getvalue())

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
        app = CVApplication(self.root, stdout=self.stdout, stderr=self.stderr, input_fn=prompt)
        with self.assertRaises(CliError):
            app.run(("new", "cover", "../unsafe"))
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
            ["cv", "VARIANT=applied_scientist", "LOGOS=1"], verbose=False
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

    def test_edit_variant_uses_editor(self) -> None:
        source = self.root / "variants/ml_platform.tex"
        source.write_text("% variant\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"EDITOR": "code --wait"}, clear=False), mock.patch(
            "tools.cv_cli.subprocess.run"
        ) as run:
            run.return_value.returncode = 0
            self.assertEqual(self.run_app("edit", "platform"), 0)
        run.assert_called_once_with(["code", "--wait", str(source)], cwd=self.root, check=False)

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

    def test_setup_repairs_stale_symlink_and_is_idempotent(self) -> None:
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        old_target = self.root / "old-cv"
        old_target.write_text("old", encoding="utf-8")
        (bin_directory / "cv").symlink_to(old_target)
        with mock.patch.dict(os.environ, {"CV_BIN_DIR": str(bin_directory)}, clear=False), mock.patch.object(
            self.app, "_fetch_assets", return_value=0
        ), mock.patch.object(self.app, "cmd_doctor", return_value=0):
            args = argparse.Namespace(verbose=False)
            self.assertEqual(self.app.cmd_setup(args), 0)
            self.assertEqual((bin_directory / "cv").resolve(), (self.root / "cv").resolve())
            self.assertEqual(self.app.cmd_setup(args), 0)

    def test_offline_setup_installs_command_and_reports_retry(self) -> None:
        self._write_manifest(b"network payload")
        bin_directory = self.root / "bin"
        with mock.patch.dict(os.environ, {"CV_BIN_DIR": str(bin_directory)}, clear=False), mock.patch.object(
            self.app, "_download", side_effect=CliError("Unable to download: offline")
        ), mock.patch.object(self.app, "cmd_doctor", return_value=0):
            result = self.app.cmd_setup(argparse.Namespace(verbose=False))
        self.assertEqual(result, 1)
        self.assertEqual((bin_directory / "cv").resolve(), (self.root / "cv").resolve())
        self.assertIn("retry `cv assets fetch`", self.stderr.getvalue())

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
        self.assertIn("Application slugs", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
