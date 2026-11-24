.PHONY: install test eval eval-live web run tf-validate deploy deploy-dry-run

install:
	pip install -r requirements-dev.txt

test:
	python -m pytest -v

eval:
	python -m evals.offline_eval

eval-live:
	adk eval agent_app evals/golden_evalset.json --config_file_path evals/test_config.json

web:
	adk web

run:
	python -m agent_app.runner

tf-validate:
	terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate

deploy-dry-run:
	python -m agent_app.deploy --project $(PROJECT_ID)

deploy:
	PROJECT_ID=$(PROJECT_ID) ./deploy.sh
