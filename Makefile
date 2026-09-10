.DEFAULT_GOAL := help

PYTHON ?= python3
UV ?= uv
COMMIT ?= HEAD
BUILD_DIR ?= build/packages
DIST_DIR ?= dist
MATERIALS ?=

.PHONY: help package desktop verify release

help:
	@echo 'make package  Build wheel and sdist from COMMIT (default: HEAD)'
	@echo 'make desktop  Build and inventory the native portable program'
	@echo 'make verify   Run the frozen desktop acceptance runner'
	@echo 'make release MATERIALS=/path/to/distribution  Archive a desktop build'
	@echo 'Overrides: COMMIT, PYTHON, UV, BUILD_DIR, DIST_DIR'
	@echo 'Windows: use GNU Make and PYTHON=python on the native Windows host.'

package desktop verify release:
	"$(PYTHON)" scripts/make_packages.py "$@" --commit "$(COMMIT)" \
		--uv "$(UV)" --build-dir "$(BUILD_DIR)" --dist-dir "$(DIST_DIR)" \
		--materials "$(MATERIALS)"
