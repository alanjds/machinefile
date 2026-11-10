# Distribution targets. Day-to-day dev uses `uv` directly (see README).
UV    ?= uv

.PHONY: test publish-testpypi publish-pypi dist clean

test:
	$(UV) run pytest tests/

# Upload lanes. Both read credentials from ~/.pypirc ([testpypi]/[pypi]
# sections); the CI workflows (OIDC) are the canonical path, these are
# for manual pushes from a dev box.
publish-testpypi: dist
	uvx twine check dist/*
	uvx twine upload --verbose --repository testpypi --config-file ~/.pypirc dist/*

publish-pypi: dist
	uvx twine check dist/*
	uvx twine upload --verbose --repository pypi --config-file ~/.pypirc dist/*

dist: clean
	$(UV) build --out-dir dist

clean:
	rm -rf build dist
