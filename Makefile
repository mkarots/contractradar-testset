# Use the project venv when it exists, so `make` does not need `source .venv/bin/activate`.
# Expanded when a recipe runs, so `make install cases` sees the venv install just created.
PY ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

.PHONY: install cases text naive score site all test server-deps serve clean

all: cases text naive site

.venv/bin/python:
	@if command -v uv >/dev/null 2>&1; then uv venv .venv; \
	else python3 -m venv .venv; fi

install: .venv/bin/python
	@if command -v uv >/dev/null 2>&1; then \
		uv pip install --python .venv/bin/python -r requirements.txt; \
	else \
		.venv/bin/python -m pip install -r requirements.txt; \
	fi
	@echo "Next: make cases"

cases:
	$(PY) -m crtest.gen --out cases

text:
	$(PY) -m crtest.dump_text --cases cases

naive:
	$(PY) -m crtest.extract_naive --cases cases --out results/naive_text.json
	$(PY) -m crtest.score --cases cases --pred results/naive_text.json

score:
	for f in results/*.json; do case $$f in *.scored.json) ;; *) $(PY) -m crtest.score --cases cases --pred $$f ;; esac; done

site:
	$(PY) -m crtest.site --cases cases --results results --out site/dist

test: .venv/bin/python
	@if command -v uv >/dev/null 2>&1; then \
		uv pip install --python .venv/bin/python -r requirements-dev.txt; \
	else \
		.venv/bin/python -m pip install -r requirements-dev.txt; \
	fi
	$(PY) -m pytest -q tests

server-deps: .venv/bin/python
	@if command -v uv >/dev/null 2>&1; then \
		uv pip install --python .venv/bin/python -r requirements-server.txt; \
	else \
		.venv/bin/python -m pip install -r requirements-server.txt; \
	fi

serve: server-deps
	$(PY) -m crtest.server --root . --store submissions --port 8765

clean:
	rm -rf site/dist
