.PHONY: build run-quick run-pilot report clean

# Build the Docker image
build:
	docker build -t ctfbase-bench .

# Quick smoke test: one easy task, one trial
run-quick:
	python3 runners/run.py \
		--models google/gemma-4-31b-it \
		--tasks dynastic \
		--trials 1 \
		--yes

# Pilot run: all tasks, 2 trials
run-pilot:
	python3 runners/run.py \
		--trials 2 \
		--yes

# Run with custom agents
run-custom:
	python3 runners/run.py \
		--agents-dir agents/custom \
		--trials 2 \
		--yes

# Regenerate HTML report from latest results
report:
	@latest=$$(ls -t results/*_results.json 2>/dev/null | head -1); \
	if [ -z "$$latest" ]; then echo "No results found in results/"; exit 1; fi; \
	python3 grading/report_html.py "$$latest"

# Clean up Docker resources
clean:
	docker system prune -f
	rm -rf logs/ results/
