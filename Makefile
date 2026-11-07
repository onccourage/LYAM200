.PHONY: install run test fetch batch seed export-seed rehearse reset-db results-dev results-prod

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

seed:
	python scripts/seed.py import $(or $(GROUP),DEV)

export-seed:
	python scripts/seed.py export $(or $(GROUP),DEV)

rehearse:
	python scripts/rehearse_control_hour.py $(or $(GROUP),DEV) p1-ru-001 p1-kz-001 p1-en-005 p4-ru-001

reset-db:
	rm -f data/cqa.db data/cqa.db-wal data/cqa.db-shm

# Сдача результатов (CQA_Student_Guide_Appendix_Results). TEAM_NAME — в .env или make TEAM_NAME="..."
results-dev:
	python scripts/export_results.py dev

results-prod:
	python scripts/export_results.py prod --group $(GROUP)
