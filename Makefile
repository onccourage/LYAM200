.PHONY: install run test fetch

install:
	pip install -r requirements.txt

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	python -m pytest -q

fetch:
	python scripts/fetch_dataset.py $(or $(GROUP),DEV)
