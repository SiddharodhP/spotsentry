# Architecture

SpotSentry keeps a long job's progress safe on spot instances. A small library runs inside the job,
watches for the cloud's warning that the instance is being taken back, and saves as much state as
fits in the time left. A replacement instance reads that state from object storage and carries on.

```
  Pre-run Planner (CLI)                 AWS, or local Docker
  reads advisor data,        ┌──────────────────────────────────────────────┐
  placement scores and  plan │  Spot instance, started by an Auto Scaling   │
  spot prices           ───► │  group (one instance, Capacity Rebalancing)  │
                             │                                              │
                             │  User job  ──safe-point calls──► SpotSentry  │
                             │  (workload  ◄─restore or stop──   runtime    │
                             │   + adapter)                        │    ▲    │
                             │                                     │    │    │
                             │                          checkpoints│    │polls│
                             │                     manifest, events│    │    │
                             │                       lease, status │    │    │
                             │                                     ▼    │    │
                             │       S3 bucket (MinIO locally)    IMDS (AEMM │
                             │                 ▲                   locally)  │
                             └─────────────────┼────────────────────────────┘
                                               │ read only
                                           Dashboard (Streamlit)
```

## What happens during an interruption

1. **Normal running.** At each safe point the job calls `sentry.step()`. A background watcher polls
   the metadata service every 2 seconds. Periodic checkpoints are captured quickly in memory and
   uploaded in the background, so training barely pauses.
2. **Rebalance signal, if AWS sends one.** SpotSentry takes one early checkpoint. If a replacement is
   already waiting for the lease, this worker saves and hands over early.
3. **Notice.** Up to two minutes before the instance goes. The next `step()` call raises
   `SpotInterrupted` inside the job, at a point where the state is consistent.
4. **Deadline-aware choice.** The selector predicts how long each tier would take (capture, serialize,
   upload) from measured throughput and picks the largest that fits in the time left, or skips.
5. **Save and exit.** A checkpoint counts only once the manifest points to it. The process exits 75
   if the emergency save succeeded and 76 if it was skipped.
6. **Recovery.** The Auto Scaling group starts a new instance. It takes the lease, reads the manifest,
   falls back through history if the newest checkpoint is damaged, and resumes.

## Components

| Component | File | What it does |
|---|---|---|
| Public API | `spotsentry/core.py` | `SpotSentry`: `start`, `step`, `end_epoch`, `complete` |
| Metadata client and watcher | `metadata.py`, `watcher.py` | IMDSv2 tokens, notice and rebalance polling |
| Checkpoint manager | `checkpoint.py` | Two-step write, manifest, history, fallback restore, pruning |
| Deadline selector | `deadline.py` | Picks full, light, tiny or skip for the time left |
| Uploader | `uploader.py` | Two-phase checkpoints: quick capture, background upload |
| Interval tuner | `interval.py` | Young's interval, `sqrt(2CM)`, from measured checkpoint cost |
| Lease | `lease.py` | One worker per job, using S3 conditional writes; fencing and handover |
| Stores | `store/` | S3 and MinIO, local folder, throttling for experiments |
| Adapters | `adapters/` | What each tier holds, for PyTorch, item lists and your own job |
| Events | `events.py` | Structured log, written to stdout and to the store |
| Planner | `planner/` | Predicts run time, risk and cost, then recommends instance and interval |
| Dashboard | `dashboard/app.py`, `live.py` | Reads only from the store, so works the same locally and on AWS |
| Local lab | `local/` | Docker Compose, AWS metadata mock, scenario orchestrator |
| Experiments | `experiments/` | E1 to E6, plots and tables for the paper |
| AWS infrastructure | `infra/` | CloudFormation, deploy and teardown |

## What a store holds

```
jobs/<job_id>/
  latest.json              manifest: newest checkpoint, its checksum, and history
  checkpoints/000012_full_e3_s150.ckpt
  events/<instance>_<pid>.jsonl
  lease.json               who owns the job, and until when
  status.json              completed, or failed
  plan.json                the planner's recommendation, if saved
```

## Why it can be trusted after a crash

- **The manifest is the commit point.** A checkpoint object is written first, then the manifest. If
  the process dies between the two, the manifest still points to the previous good checkpoint.
- **Every checkpoint has a checksum.** A damaged one is detected on restore and history is tried.
- **Only one worker writes.** Replacement instances wait for the lease. A worker that lost its lease
  stops writing, so a slow old worker cannot overwrite a newer one's progress.
- **Sequence numbers only go up.** The manifest refuses to move backwards.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Job finished |
| 75 | Interrupted, emergency checkpoint saved |
| 76 | Interrupted, no checkpoint fitted; resume from the last one |
| 1 | Error |
