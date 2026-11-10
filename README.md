# SpotSentry

Run long jobs on cheap AWS spot instances without losing progress.

Spot instances cost up to 90% less than on-demand, but AWS can take them back at any time with
2 minutes' warning. SpotSentry is a small Python framework that covers a job's whole life:

| Stage | What happens |
|---|---|
| **Before** | The Pre-run Planner profiles the job, predicts its run time, estimates interruption risk and cost, and recommends where to run and how often to checkpoint |
| **During** | Progress is saved periodically (interval from Young's formula) and immediately on the first warning, choosing a checkpoint size that is sure to finish before the deadline |
| **After** | A replacement instance restores the latest valid checkpoint from S3 and continues exactly where the job stopped |
| **Throughout** | A live dashboard, plus reproducible experiments for the paper |

Team: Siddharodh Punneshetty (1RV24CS272), Sourabha (1RV24CS279), Suprit Raju Halingali (1RV24CS289).
Cloud Computing (IS355TBD), RV College of Engineering. Full specification: [SPOTSENTRY_SPEC.md](SPOTSENTRY_SPEC.md).

## Quick start

You need Python 3.11+ and, for the full local stack, Docker Desktop. All commands are for a
terminal at the project root and work on Windows, macOS and Linux.

1. Create the environment. On Windows:

   ```bash
   python -m venv .venv
   ```

   ```bash
   .venv\Scripts\activate
   ```

   ```bash
   pip install -r requirements.txt -r requirements-ml.txt
   ```

   ```bash
   pip install -e . --no-deps
   ```

   Then do the steps in [Every new terminal](#every-new-terminal) below. You will do those in
   every terminal you open from now on.

2. Check it works (about 1 minute, no Docker or AWS needed):

   ```bash
   pytest -m "not integration"
   ```

3. Start MinIO (the local stand-in for S3):

   ```bash
   python scripts/setup_local.py
   ```

   Official MinIO images were withdrawn when the project was archived, so this uses the image named
   by `MINIO_IMAGE` in `.env`, by default the community-maintained `pgsty/minio`. See
   [docs/NOTES.md](docs/NOTES.md).

4. Run the demo: two simulated interruptions, then the job completes (about 3 minutes):

   ```bash
   python scripts/demo_local.py
   ```

   It prints exit codes `[75, 75, 0]` and `Hash chain: MATCHES an uninterrupted run`. If Docker is
   not running it automatically uses local processes instead; force a backend with
   `--backend docker` or `--backend process`.

5. Watch it live. In a second terminal, while the demo runs:

   ```bash
   spotsentry dashboard --job jobs/synthetic_demo.yaml
   ```

   and open http://localhost:8501. The dashboard reads from MinIO, so use the Docker backend for the
   demo. The local-process backend keeps its data in a temporary folder.

## Every new terminal

Every command in this project, and in the guides under `docs/`, runs from the project folder with
the virtual environment active. A new terminal starts with neither, so do these three steps each
time you open one, including the extra terminals for the dashboard and the AWS demo.

1. **Go to the project folder**, the one holding this README. On the team laptop that is `D:\CCel`:

   ```bash
   cd D:\CCel
   ```

2. **Activate the virtual environment.** In PowerShell:

   ```bash
   .venv\Scripts\Activate.ps1
   ```

   In Command Prompt, use `.venv\Scripts\activate.bat`. On macOS or Linux, `source .venv/bin/activate`.

3. **Check.** The prompt now starts with `(.venv)`, for example `(.venv) PS D:\CCel>`. This
   should print `ready`:

   ```bash
   python -c "import boto3, spotsentry; print('ready')"
   ```

The `aws` command is installed for the whole computer, so it works either way. `python`,
`pytest` and `spotsentry` need the environment.

| What you see | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'boto3'` (or `spotsentry`, `torch`) | Environment not active | Step 2 |
| `spotsentry : The term 'spotsentry' is not recognized` | Environment not active | Step 2 |
| `can't open file '...\scripts\demo_aws.py'` | Wrong folder | Step 1 |
| `running scripts is disabled on this system` | PowerShell blocks activation scripts | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then step 2 |

## Using it in your own job

About 3 lines plus an adapter:

```python
from spotsentry import SpotSentry, SpotInterrupted
from spotsentry.adapters import TorchTrainerAdapter

sentry = SpotSentry.from_config("jobs/cifar_local.yaml")
adapter = TorchTrainerAdapter(model, optimizer, base_seed=42)

start = sentry.start(adapter)     # takes the lease, restores the latest checkpoint if any
try:
    for epoch in range(start.epoch, EPOCHS):
        first = start.step if epoch == start.epoch else 0
        for step, batch in adapter.iter_batches(dataset, epoch, first, batch_size=128):
            train_step(batch)
            sentry.step(epoch=epoch, step=step + 1)   # safe point: may save, or stop the job
        sentry.end_epoch(epoch=epoch + 1)
    sentry.complete(result={"accuracy": accuracy})
except SpotInterrupted as e:
    sentry.exit_interrupted(e)        # exit code 75 (saved) or 76 (nothing new saved)
```

Or implement the small `Workload` class and run `spotsentry run --job your.yaml`. See
[workloads/](workloads/) for three examples.

## Commands

| Command | What it does |
|---|---|
| `spotsentry run --job FILE` | Run a job, resuming if a checkpoint exists |
| `spotsentry plan --job FILE [--offline] [--write]` | Recommend instance type, zone, interval, spot or on-demand |
| `spotsentry profile --job FILE` | Measure step time and checkpoint sizes on this machine |
| `spotsentry status --job FILE` | Show state and latest checkpoint |
| `spotsentry dashboard --job FILE` | Open the live dashboard |
| `spotsentry experiment --config FILE` | Run an experiment |

Exit codes: `0` completed, `75` interrupted and saved, `76` interrupted with nothing new saved,
`1` error.

Any config value can be overridden from the environment:
`SPOTSENTRY_DEADLINE__OVERRIDE_SECONDS=8` sets `deadline.override_seconds`.

## Scenarios to try

```bash
python scripts/demo_local.py --scenario local/scenarios/tight_deadline.yaml
```

```bash
python scripts/demo_local.py --scenario local/scenarios/skip_deadline.yaml
```

```bash
python scripts/demo_local.py --scenario local/scenarios/rebalance.yaml
```

## Experiments

To run everything, which takes several hours (close other programs, as the largest states need
several GB of memory):

```bash
python experiments/run_all.py
```

Or one at a time:

```bash
python experiments/run.py --config experiments/configs/e1.yaml
```

```bash
python experiments/plots.py
```

```bash
python experiments/analyze.py
```

Results go to `experiments/results/` and `docs/RESULTS.md`.

## Real AWS

Read [docs/AWS_STEPS.md](docs/AWS_STEPS.md) first. Deploying creates paid resources. It only happens
when you run `python infra/deploy.py --yes` yourself.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the pieces fit together
- [docs/DEMO.md](docs/DEMO.md): the final review demo script
- [docs/AWS_STEPS.md](docs/AWS_STEPS.md): deploying to AWS step by step
- [docs/RESULTS.md](docs/RESULTS.md): experiment results
- [docs/NOTES.md](docs/NOTES.md): what was verified, and decisions that differ from the spec

## Syllabus mapping

| Unit | Concepts used |
|---|---|
| I | Public cloud deployment model; IaaS service model; cloud characteristics (on-demand, pay-per-use, measured service); Identity as a Service via IAM roles |
| II | Cloud computing stack; connecting to the cloud via APIs; service-oriented architecture (metadata service, S3, EC2/Auto Scaling APIs); monitoring |
| III | Cloud storage (Amazon S3, S3-compatible MinIO as a standard interface); web APIs (IMDS, S3 REST); network and security considerations |
| IV | Virtualization (spot VMs on the AWS hypervisor); machine imaging (AMI/launch template); porting applications between instances; **capacity planning**: baselines, metrics, network capacity (checkpoint size / bandwidth vs deadline), scaling (Auto Scaling group); the Pre-run Planner |
| V | Application development, troubleshooting and management (CLI, logging, dashboard) |
