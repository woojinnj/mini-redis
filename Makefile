.PHONY: test server demo benchmark gui
PYTHON ?= python3

test:
	$(PYTHON) -m unittest discover -v

server:
	$(PYTHON) -m mini_redis.server --snapshot data/snapshot.json

demo:
	$(PYTHON) -m mini_redis.demo

benchmark:
	$(PYTHON) -m mini_redis.benchmark

gui:
	$(PYTHON) -m mini_redis.gui
