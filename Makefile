.PHONY: eval eval-build eval-check eval-baseline

eval-build:
	redibis pii eval-build --corpus tests/data/text_pii/corpora --out tests/data/text_pii/datasets

eval-check:
	pytest tests/test_pii_eval_corpus_sync.py -q --tb=short

# Pin --rules so the run does not pick up whatever is in global_settings.pii_text_rules.
# --baseline is included only when reports/baseline.json already exists (gitignored).
EVAL_BASELINE_FLAG=$(shell test -f reports/baseline.json && echo --baseline reports/baseline.json)

eval: eval-build
	redibis pii eval --dataset tests/data/text_pii/datasets --recursive \
	  --rules configs/text_rules.default.yaml \
	  --normalization v1 --tier strict,value,overlap,type \
	  --gate-file tests/data/text_pii/gates/all.yaml \
	  $(EVAL_BASELINE_FLAG) \
	  --out-dir reports/eval

eval-baseline: eval-build
	redibis pii eval --dataset tests/data/text_pii/datasets --recursive \
	  --rules configs/text_rules.default.yaml -o reports/baseline.json
