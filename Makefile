.PHONY: frontend-install frontend-build dev release release-deterministic

SPECOPS_DATABASE_URL ?= sqlite:///./specops-workshop.sqlite
WORKSHOP_DATABASE_URL ?= sqlite:///./workshop-sessions.sqlite
export SPECOPS_DATABASE_URL
export WORKSHOP_DATABASE_URL

frontend-install:
	cd frontend && npm install

frontend-build: frontend-install
	cd frontend && npm run build

dev: frontend-build
	.venv/bin/uvicorn specops_workshop.api:create_app --factory --host 127.0.0.1 --port 8000

release-deterministic:
	.venv/bin/python scripts/verify_workshop_release.py

release:
	.venv/bin/python scripts/verify_workshop_release.py --live
