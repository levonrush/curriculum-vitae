SHELL := /bin/sh

LATEXMK := latexmk
BUILD_DIR := build
VARIANT ?= applied_scientist
APP ?= template
LOGOS ?= 1
VERBOSE ?= 0

CV_VARIANTS := applied_scientist ml_platform research_engineer
CV_LABEL_applied_scientist := Applied_Scientist
CV_LABEL_ml_platform := ML_Platform
CV_LABEL_research_engineer := Research_Engineer

SHOW_LOGOS := $(if $(filter 0 false no,$(LOGOS)),false,true)
NO_LOGO_SUFFIX := $(if $(filter false,$(SHOW_LOGOS)),_No_Logos,)
CV_JOB := Levon_Rush_CV_$(CV_LABEL_$(VARIANT))$(NO_LOGO_SUFFIX)
COVER_LABEL := $(if $(filter template,$(APP)),Template,$(subst -,_,$(APP)))
COVER_JOB := Levon_Rush_Cover_Letter_$(COVER_LABEL)
LATEXMK_QUIET := $(if $(filter 1 true yes,$(VERBOSE)),,-silent)
LATEXMK_REDIRECT := $(if $(filter 1 true yes,$(VERBOSE)),,>/dev/null)
LATEX_FLAGS := -xelatex -interaction=nonstopmode -halt-on-error -file-line-error -outdir=$(BUILD_DIR)

export SOURCE_DATE_EPOCH ?= 1704067200
export FORCE_SOURCE_DATE := 1
export TZ := UTC

.PHONY: all cvs covers cv cover cover-template check proof reproducibility clean

all: cvs cover-template

cvs:
	@set -e; for variant in $(CV_VARIANTS); do \
		$(MAKE) --no-print-directory cv VARIANT=$$variant LOGOS=$(LOGOS) VERBOSE=$(VERBOSE); \
	done

covers: cover-template

cv:
	@if [ -z "$(CV_LABEL_$(VARIANT))" ]; then \
		echo "Unknown CV variant: $(VARIANT)" >&2; \
		echo "Choose one of: $(CV_VARIANTS)" >&2; \
		exit 2; \
	fi
	@mkdir -p $(BUILD_DIR)
	@echo "Building $(CV_JOB).pdf"
	@$(LATEXMK) $(LATEXMK_QUIET) $(LATEX_FLAGS) -jobname=$(CV_JOB) \
		-usepretex='\def\VariantContentFile{variants/$(VARIANT).tex}\def\ShowLogos{$(SHOW_LOGOS)}' \
		src/cv.tex $(LATEXMK_REDIRECT) || { echo "Build failed; see $(BUILD_DIR)/$(CV_JOB).log" >&2; exit 1; }

cover:
	@if [ ! -f "applications/$(APP).tex" ]; then \
		echo "Cover application not found: applications/$(APP).tex" >&2; \
		exit 2; \
	fi
	@mkdir -p $(BUILD_DIR)
	@echo "Building $(COVER_JOB).pdf"
	@$(LATEXMK) $(LATEXMK_QUIET) $(LATEX_FLAGS) -jobname=$(COVER_JOB) \
		-usepretex='\def\ApplicationFile{applications/$(APP).tex}\def\ShowLogos{false}' \
		src/cover_letter.tex $(LATEXMK_REDIRECT) || { echo "Build failed; see $(BUILD_DIR)/$(COVER_JOB).log" >&2; exit 1; }

cover-template:
	@$(MAKE) --no-print-directory cover APP=template VERBOSE=$(VERBOSE)

check:
	@python3 -m unittest discover -s tests
	@$(MAKE) --no-print-directory reproducibility VERBOSE=$(VERBOSE)
	@$(MAKE) --no-print-directory all VERBOSE=$(VERBOSE)
	@$(MAKE) --no-print-directory cvs LOGOS=0 VERBOSE=$(VERBOSE)
	@python3 scripts/check_assets.py
	@python3 scripts/check_pdf.py --proof

proof: all
	@python3 scripts/check_pdf.py --proof

reproducibility:
	@tmp=$$(mktemp -d); \
	trap 'rm -rf "$$tmp"' EXIT HUP INT TERM; \
	$(MAKE) --no-print-directory clean >/dev/null; \
	$(MAKE) --no-print-directory all VERBOSE=$(VERBOSE) >/dev/null; \
	python3 -c 'import hashlib, pathlib; files=sorted(pathlib.Path("build").glob("*.pdf")); print("\n".join(f"{p.name} {hashlib.sha256(p.read_bytes()).hexdigest()}" for p in files))' > "$$tmp/first"; \
	$(MAKE) --no-print-directory clean >/dev/null; \
	$(MAKE) --no-print-directory all VERBOSE=$(VERBOSE) >/dev/null; \
	python3 -c 'import hashlib, pathlib; files=sorted(pathlib.Path("build").glob("*.pdf")); print("\n".join(f"{p.name} {hashlib.sha256(p.read_bytes()).hexdigest()}" for p in files))' > "$$tmp/second"; \
	diff -u "$$tmp/first" "$$tmp/second" >/dev/null || { echo "PDF builds are not reproducible" >&2; diff -u "$$tmp/first" "$$tmp/second" >&2; exit 1; }; \
	echo "Reproducibility check passed"

clean:
	@rm -rf $(BUILD_DIR)
	@echo "Removed generated documents from $(BUILD_DIR)/"
