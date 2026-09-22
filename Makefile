.PHONY: docker lint format check test install install-dev

PORT ?= 7860
DOCS ?= $(CURDIR)
PDF  ?=

# DOCS is always mounted read-only at /docs; PDF names a file inside it and
# expands to nothing when unset, so the same line covers both cases.
docker:
	docker build -t pdf-active-reader .
	docker run --rm -t -p $(PORT):$(PORT) -e PORT=$(PORT) -v "$(DOCS)":/docs:ro pdf-active-reader $(PDF:%=/docs/%)

lint:
	ruff check . --fix

format:
	ruff format .

check: lint format test

test:
	python tests/test_pipeline.py

install:
	pip install -e .
 
install-dev:
	pip install -e ".[dev]"

install-full:
    pip install -e ".[all]"