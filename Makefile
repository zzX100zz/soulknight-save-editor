PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: help setup venv airlift info backup unlock devices test dmg clean

help:
	@echo "make setup    create .venv, install requirements, clone + build AirLift"
	@echo "make devices  list paired iPhones"
	@echo "make info     read the save from the phone (changes nothing)"
	@echo "make backup   keep a copy of the save (Mac + phone)"
	@echo "make unlock   unlock everything"
	@echo "make test     run the offline test suite"
	@echo "make dmg      build a DMG for releases (macOS)"

setup: venv airlift
	$(BIN)/python run.py setup

venv: $(BIN)/python
$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements.txt

airlift:
	$(BIN)/python -c "from sksave import device; device.ensure_airlift()"

devices: venv
	$(BIN)/python run.py devices

info: venv
	$(BIN)/python run.py info

backup: venv
	$(BIN)/python run.py backup

unlock: venv
	$(BIN)/python run.py unlock

test: venv
	$(BIN)/python -m pytest -q tests

app: ## build dist/SoulKnightSaveEditor.app
	bash scripts/build_app.sh
	open dist/SoulKnightSaveEditor.app

dmg: venv
	./scripts/build_dmg.sh

clean:
	rm -rf build dist work .pytest_cache
