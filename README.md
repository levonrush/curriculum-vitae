# Levon Rush — CV and cover-letter system

A local, version-controlled XeLaTeX system for three-page A4 technical CVs and
one-page cover letters. The friendly `cv` command wraps all document tooling;
ordinary use does not require knowing LaTeX or Make.

## Quick start

```sh
./cv setup
cv build
cv view
```

`cv setup` is safe to run again. It checks the toolchain, installs a symlink at
`~/.local/bin/cv`, and obtains the official employer/institution marks used by
the logo-enabled CV. Once setup has completed, builds work offline.

The default `cv view` target is the applied-scientist CV. Generated files live
under `build/` and are deliberately not committed.

## Everyday commands

```sh
cv build                         # all CVs and the cover template
cv build cv                      # all CV variants
cv build applied                 # applied-scientist CV
cv build platform                # ML-platform CV
cv build research                # research-engineer CV
cv build cv --no-logos           # text-only CV variants

cv view                          # build if stale, then open the default CV
cv view platform                 # open one variant
cv view all                      # open build/ in Finder

cv list                          # variants, applications, and build state
cv doctor                        # local prerequisite and asset report
cv check                         # compile and run all quality gates
cv clean                         # remove build/ and stray TeX files; preserve assets
```

Run `cv help` or `cv <command> --help` for complete usage. Add `--verbose` to a
build when the full XeLaTeX output is useful.

## Tailoring a cover letter

Create an application configuration, edit its role-specific prose, then build
and view it:

```sh
cv new cover microsoft-principal-applied-scientist
cv edit cover microsoft-principal-applied-scientist
cv build cover microsoft-principal-applied-scientist
cv view cover microsoft-principal-applied-scientist
```

An application supplies the company, role, hiring manager, explicit date,
opening, two or three selected evidence blocks, organisation-specific fit, and
closing. The permanent evidence library covers applied science, ML platforms,
research engineering, principal technical practice, and infrastructure and
utilities. Application builds reject unresolved placeholders and must fit one
A4 page.

## CV variants

- `applied_scientist` is the balanced default: modelling, platform engineering,
  operational deployment, and technical leadership.
- `ml_platform` prioritises governed delivery, reusable infrastructure,
  production separation, and self-service scientific computing.
- `research_engineer` gives earlier prominence to the PhD, sampling-regime
  research, uncertainty, constraints, and live-industry validation.

Each variant is exactly three A4 pages. Employer names always remain readable
text. The decorative marks use their official supplied colours (including the
University's black-and-white primary mark) and can be disabled without changing
the extracted content.

## Updating content

Shared facts and prose live under `src/content/`; variant wrappers under
`variants/` select and order that material. Contact details and role dates are
centralised so that they are not independently copied between documents.

Before strengthening impact language, review [the content-gap checklist](docs/content-gaps.md).
Do not invent metrics or promote planned/pilot work to production. The original
source files and migration decisions are documented in
[the source audit](docs/source-audit.md); the originals are not stored here
because they contain third-party personal data and hidden Word metadata.

## Logos and third-party marks

Official marks are cached under `.vendor/logos/` and are ignored by Git. Their
source URLs, hashes, display constraints, and ownership notices are tracked in
`assets/logo_sources.json` and `THIRD_PARTY_NOTICES.md`.

```sh
cv assets status
cv assets fetch
```

Logo-enabled builds are for factual identification of employment and education.
Confirm any required brand approval before external distribution. Use
`cv build cv --no-logos` when a portal or recipient prefers a strictly text-only
document.

The marks are compact 5.5 mm-high accents beside the relevant organisation
names rather than page headers. This requested inline treatment is below nib's
and the University's published print minima. It is therefore a local layout
preview, not a claim of brand compliance. The University's approved horizontal
artwork remains staff-gated, so the exact public square mark is used rather than
a third-party recreation. See `THIRD_PARTY_NOTICES.md` before distributing a
logo-enabled PDF.

## Quality checks

`cv check` validates all documents and the command-line interface. Among other
things it checks:

- three A4 pages per CV and one A4 page per cover letter;
- embedded fonts with Unicode maps;
- sensible `pdftotext` reading order;
- identical text in logo and no-logo variants;
- valid links and sanitised PDF metadata;
- absence of overflow, unresolved placeholders, old referee data, prohibited
  clichés, and the retired personal domain;
- legible colour and greyscale proof renders;
- reproducible output under the pinned build environment.

The checks use `pdfinfo`, `pdffonts`, `pdftotext`, and `pdftoppm` from Poppler.
They are strong ATS proxies, not a guarantee for every proprietary recruitment
system.

## Toolchain

- Python 3.10 or newer, standard library only for the CLI.
- XeLaTeX and `latexmk` (TeX Live 2025 is the tested baseline).
- GNU Make.
- Poppler command-line tools.
- macOS Quick Look (`qlmanage`) for the pinned University SVG conversion during
  first-time asset setup.
- macOS `open` for viewing; `xdg-open` is used when available on Linux.

The document source uses a small set of standard TeX packages and vendored IBM
Plex Sans files. It does not require Overleaf, Word, a browser-print engine,
YAML, or a Python template framework.

## Repository policy

- `build/`, `.vendor/`, original DOCX/PDF files, and local editor state are ignored.
- Font files and their licence are committed for deterministic typography.
- Employer/institution marks are fetched locally rather than redistributed.
- No remote repository is configured or published by the tooling.
