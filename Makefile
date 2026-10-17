PYTHON ?= python3

.PHONY: test validate build
test:
	$(PYTHON) -m unittest discover -s tests -v

validate:
	$(PYTHON) tools/build_skill.py --check-only

build: test validate
	$(PYTHON) tools/build_skill.py
