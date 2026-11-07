.PHONY: install run test fetch batch

install:
	pip install -r requirements.txt

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	python -m pytest -q

batch:
	python scripts/run_batch.py $(or $(GROUP),DEV)

fetch:
	python scripts/fetch_dataset.py $(or $(GROUP),DEV)
