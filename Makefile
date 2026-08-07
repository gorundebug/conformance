.PHONY: tracing metrics all clean

all: tracing metrics

tracing:
	python3 tracing/run.py

metrics:
	python3 metrics/run.py

clean:
	rm -rf .artifacts
