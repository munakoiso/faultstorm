.PHONY: help lint lint-fix mypy test clean install-dev venv activate test-up test-down test-build

help:
	@echo "Available targets:"
	@echo "  make venv           - Create virtual environment"
	@echo "  make activate       - Show command to activate virtual environment"
	@echo "  make lint          - Run linters (flake8, isort, black --check)"
	@echo "  make lint-fix      - Fix linting issues (isort, black)"
	@echo "  make mypy          - Run mypy type checker"
	@echo "  make test          - Run tests with pytest"
	@echo "  make clean         - Clean up temporary files"
	@echo "  make install-dev   - Install development dependencies"

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
ACTIVATE := source $(VENV)/bin/activate

COMPOSE_FILE := tests/docker-compose.yml
BEHAVE_ARGS ?=
PYTHONPATH := $(CURDIR)
PYTHON ?= /usr/bin/python3

venv:
	@echo "Creating virtual environment..."
	python3 -m venv $(VENV)
	@echo "Virtual environment created at $(VENV)"
	@echo "Run 'make install-dev' to install dependencies"
	@echo "Run 'make activate' to activate the virtual environment"

activate:
	@echo "Run the following command to activate the virtual environment:"
	@echo "  $(ACTIVATE)"

lint:
	@echo "Running flake8..."
	$(VENV)/bin/flake8 faultstorm/ --max-line-length=100 --extend-ignore=E203,W503
	@echo "Running isort --check..."
	$(VENV)/bin/isort --check-only --profile black faultstorm/
	@echo "Running black --check..."
	$(VENV)/bin/black --check --line-length=100 faultstorm/

lint-fix:
	@echo "Running isort..."
	$(VENV)/bin/isort --profile black faultstorm/
	@echo "Running black..."
	$(VENV)/bin/black --line-length=100 faultstorm/

mypy:
	@echo "Running mypy..."
	$(VENV)/bin/mypy faultstorm/ --ignore-missing-imports --strict

clean:
	@echo "Cleaning up..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	find . -type f -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "dist" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "build" -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(VENV)

install-dev:
	@echo "Installing development dependencies..."
	$(PIP) install -r requirements-dev.txt
	$(PIP) install -e .

# Copy bundled scripts into Docker build context before building
test-build:
	cp faultstorm/scripts/process_freezer.sh tests/docker/process_freezer.sh
	PATH="/opt/homebrew/bin:/usr/local/bin:$$PATH" docker compose -f $(COMPOSE_FILE) build

# Start test containers
test-up: test-build
	PATH="/opt/homebrew/bin:/usr/local/bin:$$PATH" docker compose -f $(COMPOSE_FILE) up -d --wait

# Stop and remove test containers
test-down:
	PATH="/opt/homebrew/bin:/usr/local/bin:$$PATH" docker compose -f $(COMPOSE_FILE) down -v --remove-orphans

# Run behave tests (containers are managed by environment.py)
test:
	cd tests && PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m behave $(BEHAVE_ARGS)

# Run a specific feature
test-feature:
	cd tests && PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m behave features/$(FEATURE).feature $(BEHAVE_ARGS)
