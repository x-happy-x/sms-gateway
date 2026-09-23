PYTHON ?= python3

.PHONY: test run

test:
	$(PYTHON) -m unittest discover -s tests

# Local UI against a scratch archive; the router in .local/dev/config.json must stay unreachable.
run:
	SMSGW_HOME=.local/dev $(PYTHON) server.py
