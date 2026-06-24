# Minimal makefile for Sphinx documentation (Linux/macOS).
# Usage from the docs/ directory:
#   make html        build the HTML site into docs/_build/html
#   make clean       remove the build output
#   make help        list all available builders
#
# This is only a convenience wrapper; the equivalent direct call is
#   sphinx-build -b html . _build/html
# (Windows users would use the separate make.bat, which is intentionally not
# provided here.)

SPHINXOPTS    ?=
SPHINXBUILD   ?= sphinx-build
SOURCEDIR     = .
BUILDDIR      = _build

help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help Makefile

# Route any other target (html, clean, linkcheck, ...) to sphinx-build.
%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
