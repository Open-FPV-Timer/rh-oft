PYTHON ?= python3

.PHONY: test rh-test zip clean

test: ## Run the plugin tests against a fake OFT Timer
	PYTHONPATH=.:tests:tests/stubs $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

rh-test: ## Run the plugin inside RotorHazard in Docker, against `oftd --fake`
	tests/rh/run.sh

zip: ## Build dist/rh_oft.zip for dropping into ~/rh-data/plugins/
	rm -rf dist && mkdir -p dist/rh_oft
	cp -r rh_oft/*.py dist/rh_oft/ && cp manifest.json LICENSE dist/rh_oft/
	cd dist && zip -qr rh_oft.zip rh_oft && rm -rf rh_oft
	@echo "dist/rh_oft.zip"

clean:
	rm -rf dist **/__pycache__
