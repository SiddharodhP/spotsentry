# Notes

Decisions, things verified against the real tools, and known limits. Write the honest version here,
because the paper and the review questions draw on it.

## Experiment status

All six experiments have full results, from a re-run on 2026-11-07 after the fixes below. Tables
are in `docs/RESULTS.md`, plots in `experiments/results/`. No trial failed. Earlier, superseded
results are kept in `experiments/results/old/` for comparison, and nothing reads them.

What the results say, honestly:

- **E1:** with interruptions every 5 minutes on average, no checkpointing takes 6x the ideal time
  and never finishes in time. Reactive, hybrid and Young are close, at 1.6x to 1.7x. Reactive is
  slightly best here because every interruption in this experiment comes with a notice.
- **E2:** deadline-aware saving met the deadline in every trial, against 0% to 67% for always
  saving full. Part of that is deliberate skips, which keep the earlier checkpoint. It still saved
  progress more often: 22% against 0% at 500 MB. Predictions were cautious, 1.28x actual on average.
- **E3:** with the full tier, final weights are identical to the uninterrupted run after 1, 2 and 3
  interruptions, for all 5 seeds. The light tier loses optimizer state and ends slightly lower.
- **E4:** prediction error 0.4% to 9.6%, on this machine only. The spec asks for 2 machines.
- **E5:** async blocks the job for 0.2 s per checkpoint at 1 GB against 21 s for sync. But the job
  is still slowed by 68% at 1 GB, most likely because serialising in a background thread competes
  with training for Python's interpreter lock. Say so rather than claiming async is free.
- **E6:** adaptive is best or within noise of the best at M = 15 and 60 minutes, but loses to a
  fixed 1-minute interval at M = 5 minutes (3.89x against 2.95x). It starts from a deliberately
  wrong 1-hour prior, weighted as 3 interruptions, and a 30-minute job is too short to unlearn it.

Avoid `python experiments/run_all.py --skip-done` after a stopped run: it treats any existing CSV
as finished, even a partial one. Name experiments with `--only` instead.

## Verified, and how

| Claim | How it was checked | When |
|---|---|---|
| Interruption notice, emergency save, exit 75, exact resume | `demo_local.py` on real worker containers with the AWS metadata mock: exit codes `[75, 75, 0]`, hash chain matches | 2026-11-06 |
| Rebalance signal arrives before the notice and triggers an early checkpoint | `rebalance.yaml` on the AWS metadata mock | 2026-11-06 |
| Deadline fallback: full does not fit, a smaller tier is saved in time | `tight_deadline.yaml` on both backends: tiny saved in 12.5 s and light in 16.6 s, both inside 30 s | 2026-11-06 |
| Nothing fits: skip and exit 76, then resume from an earlier state | `skip_deadline.yaml` on Docker | 2026-11-06 |
| Damaged newest checkpoint: restore falls back through history | Lifecycle test, both backends | 2026-11-06 |
| Killed with no notice: resumes from the last periodic checkpoint | Lifecycle test, both backends | 2026-11-06 |
| PyTorch resume is exact: same final weights after 1, 2 and 3 interruptions as without | Full E3 on CIFAR-10, 5 seeds, interruption count checked per trial | 2026-11-07 |
| Metadata mock flags and its port 1338 | `spot --help` on AEMM v1.13.0 | 2026-11-06 |
| CloudFormation template | `cfn-lint`, then deployed to ap-southeast-2 and updated in place | 2026-11-07 |
| Spot Advisor data format; on-demand prices | Downloaded from AWS's public files | 2026-11-05/06 |
| Live dashboard | Viewed in a browser against running jobs | 2026-11-06 |
| Dashboard experiments section | Viewed in a browser with all six plots | 2026-11-07 |
| Full CIFAR-10 run on a real spot instance, S3 checkpoints, lease, scale to zero on completion | `deploy.py` to Sydney, t3.small spot: 20 epochs, 80.7% test accuracy, 55 checkpoints of 7.5 MB, 0.13-0.2 s upload, under 2 ms blocking | 2026-11-07 |
| Recovery after an instance is killed with no notice | Terminated through the Auto Scaling group at epoch 18. Replacement took the lease and restored the full checkpoint at epoch 18 step 361, 88 s after the kill (boot included). Under 50 steps lost | 2026-11-07 |

## Not verified

- **A real two-minute spot notice on AWS.** It needs AWS FIS, which the Free plan denies. The real
  run covered deploy, S3 checkpoints, the lease, a kill with no notice, and completion. The notice
  and rebalance paths still rest on AWS's metadata mock.
- **`spotsentry plan` online**, which needs AWS credentials. Offline mode works.
- **E4 on a second machine**, and **E1 on CIFAR-10**. See `docs/RESULTS.md` for what did run.
- **`relative_speed.yaml` holds placeholder numbers**, until measured on real instance types.

## MinIO is archived

The spec calls for MinIO. Open-source MinIO has since been archived: the `minio/minio` and `minio/mc`
images are gone from Docker Hub, Quay refuses anonymous pulls, and `dl.min.io` answers `410 Gone`.

- `local/docker-compose.yml` now reads the image from `MINIO_IMAGE` in `.env`. The default is
  `pgsty/minio`, a community-maintained fork. On 2026-11-07 all 7 Docker tests passed with it as the
  store (`pytest -m integration`, 6.5 minutes), after that day's code changes.
- The bucket is now created from Python with boto3, so `mc` is no longer needed.
- If you want another S3-compatible server, set `MINIO_IMAGE`, or set `SPOTSENTRY_DOCKER_STORE_URL`
  and run the server yourself. It must answer on port 9000 of this machine.
- Before the fork was allowed, the Docker tests ran with `moto_server` on `127.0.0.1:9000` and
  `SPOTSENTRY_DOCKER_STORE_URL=http://host.docker.internal:9000`. Moto is a test double, not a real
  storage server, so throughput numbers from it mean little. `moto_server` needs `moto[server]`
  (it uses flask), while the requirements list only `moto[s3]`.

## Bugs that only real runs found

Worth mentioning in the paper's discussion, because unit tests passed throughout.

1. **Every PyTorch resume crashed.** The adapter's restore info repeated the `tier` field, clashing
   with the event's own. Tests used the synthetic job, which does not repeat it.
2. **A crashing worker looked like a hang.** Workers restarted until the time limit, with errors
   discarded. Experiment trials now stop and show the error.
3. **Small upload probes understate the link.** A 1 MiB upload is mostly request overhead, and measured
   41 Mbps on a 100 Mbps link, which made the selector skip tiers that fitted. The probe is now 5 MiB
   with overhead subtracted.
4. **The dashboard could go stale.** A refresh slower than the 2-second timer was cut off before
   reaching the lower sections. Data is now read on a background thread.
5. **`localhost` is slow on Windows** when a server listens on IPv4 only: 2 s per request against
   0.03 s, from the failed IPv6 attempt. Host-side code now uses `127.0.0.1`.
6. **Sync checkpoint cost was under-reported** as capture time only, which also misled the interval
   tuner.
7. **Every replacement worker forgot the checkpoint interval.** It started at the minimum interval
   and took a full checkpoint a moment after starting, to re-measure the cost. At short M this was 11
   of 17 checkpoints in one trial. Workers now start from the cost recorded by earlier workers.
8. **Many workers at once ran out of memory on Windows.** OpenBLAS reserves memory per CPU thread in
   every process that imports numpy. 34 of 75 E1 trials crashed. Trial workers now use one thread.
9. **E3 planned 2 and 3 interruptions but got 1,** because the job ended before the second notice.
   Trials now fail when the count does not match the plan.
10. **The planner's data files were never in the repository.** A `data/` line in `.gitignore`, meant
    for the CIFAR-10 download, also matched `spotsentry/planner/data/`. Everything worked locally,
    but a fresh clone on EC2 would have had no price table, no advisor snapshot and no speed table.
    The rule is now `/data/`, and a test checks these files are not ignored.

## The team's AWS account

Checked on 2026-11-07, read-only, before anything was deployed.

- The account is on the new **Free plan** and sits in an AWS-managed organization (account name
  "Just Ship It"). Organization rules (service control policies) apply on top of IAM, so
  `AdministratorAccess` does not override them.
- **Only `ap-southeast-2` (Sydney) is allowed.** EC2, S3, CloudFormation and Auto Scaling calls were
  denied in `ap-south-1`, `us-east-1`, `us-east-2`, `us-west-2` and `eu-west-1`, and allowed in Sydney.
  `aws login` named Sydney as the account's region. The project's AWS defaults were moved there from
  Mumbai, and Sydney prices and advisor data were added to the planner's tables.
- **AWS FIS is denied on the Free plan,** even in Sydney, so "Initiate interruption" needs the Paid
  plan.

## Decisions and departures from the spec

- **Experiments use `FakeIMDS`, not AEMM,** and a folder as the store. Time is scaled down so notices
  need sub-second precision, and many trials run at once. AEMM has one-second resolution. The real
  flow is covered by the Docker tests. The spec says Docker by default, so state this in the paper.
- **Bandwidth throttling** imitates the network in experiments, with a token bucket that catches up
  after Windows' sleep overshoots rather than saving idle credit.
- **Tight-deadline scenario uses 30 s, not 5 s.** With a 600 MB state, it needs enough time for at
  least one tier to fit. It accepts light or tiny, since that depends on the measured speed.
- **Instance role also has `AmazonSSMManagedInstanceCore`,** so you can open a console shell without
  an SSH port, and `ec2:DescribeAvailabilityZones`. Beyond the spec's least-privilege list.
- **Exactly three instance types.** CloudFormation cannot loop over a list.
- **Python 3.11 on EC2 comes from `uv`,** as the Ubuntu 22.04 package is a release candidate.
- **Advisor risk ranges:** the file's `max` values are 5, 11, 16, 22, 100. The planner uses the
  published label bounds (under 5%, 5-10%, 10-15%, 15-20%, over 20%) and assumes 30% for "over 20%".
- **`cifar_cnn` has a `dataset: fake` option** so tests need no download.

## Known limits, to state honestly

- **Reactive and hybrid exit right after saving,** as the spec says, and so give up the rest of the
  notice window. At short mean time between interruptions this can cost wall time.
- **Time scaling is imperfect.** Job time is divided by 30, but Python start-up is not, so restarts
  look relatively expensive in E1 and E6.
- **A checkpoint cannot be taken mid-step.** A very long step eats into the notice window.
- **The notice is not guaranteed.** AWS says two minutes, but it is best effort. Periodic checkpoints
  are the defence.
- **Experiment timing is from one Windows laptop,** with other programs running.

## Environment notes

- Windows 11, 12 logical CPUs, 15.5 GB of RAM, Python 3.13 locally, 3.11 in the worker image.
- Set `PYTHONUTF8=1` on Windows if text looks garbled.
- Docker Desktop failed to start here because of dead socket files in `%LOCALAPPDATA%\Docker\run`.
  Renaming that folder fixed it. A Docker Desktop update or a reboot may stop it recurring.
- Docker's disk image lives on C:, which is nearly full. Experiments' temporary files go on D:.
- **The Free plan only launches Free Tier eligible instance types.** The first deploy failed with
  "The specified instance type is not eligible for Free Tier" for c5, m5 and c6i. In Sydney the
  eligible x86 types with spot are c7i-flex.large, m7i-flex.large, t3.micro/small and
  t8i.micro/small. The defaults are now c7i-flex.large, m7i-flex.large and t3.small.
  `price-capacity-optimized` picked t3.small, the cheapest, with 2 GiB of memory.
- **CIFAR-10's own server is slow from AWS:** about 85 KB/s to Sydney, 30 minutes for 170 MB, and
  every replacement instance needs it before training resumes. The boot script now copies it from
  the job's bucket first (`infra/dataset_cache.py`), and the first instance uploads it if missing.
- **t3.small ran out of memory loading CIFAR-10** (exit 137). Normalising the images made four
  600 MB copies. It is now done in place: peak memory 2.2 GB to 1.2 GB on this laptop, with
  bit-identical tensors (checked with `torch.equal` on the real data). The boot script also adds a
  2 GB swap file.
