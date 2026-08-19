# Synthetic GPU Smoke Experiment

Measure matrix multiplication throughput for three square matrix sizes using a locally available framework.

Start with a CPU-only correctness check. If a CUDA device is available, compare synchronized GPU timing against CPU timing. Record the environment, warm-up policy, repetitions, raw timings, summary statistics and any unsupported configuration. Do not download datasets or model weights. Stop after the smoke-scale comparison; this example is only for validating the workflow.
