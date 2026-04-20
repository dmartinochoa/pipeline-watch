.PHONY: install test coverage cov-html lint type format check clean

install:
	pip install -e .
	pip install -r requirements-dev.in

test:
	pytest

coverage:
	pytest --cov=pipeline_watch --cov-report=term-missing --cov-report=xml

cov-html:
	pytest --cov=pipeline_watch --cov-report=html
	@echo "open htmlcov/index.html"

lint:
	ruff check pipeline_watch tests

type:
	mypy pipeline_watch

format:
	ruff format pipeline_watch tests
	ruff check --fix pipeline_watch tests

check: lint type coverage

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
