.PHONY: test test-up test-down test-build

COMPOSE_FILE := tests/docker-compose.yml
BEHAVE_ARGS ?=
PYTHONPATH := $(CURDIR)
PYTHON ?= /usr/bin/python3

# Build test containers
test-build:
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
