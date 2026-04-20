.PHONY: install test lint type format clean

install:
	pip install -e .
	pip install -r requirements-dev.in

test:
	pytest

lint:
	ruff check pipeline_watch tests

type:
	mypy pipeline_watch

format:
	ruff format pipeline_watch tests
	ruff check --fix pipeline_watch tests

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
