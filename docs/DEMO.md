# Final review demo (about 8 minutes)

## The day before

- Run the whole AWS demo once and **record the screen**, following [RECORDING.md](RECORDING.md).
- Check the local demo works, so you have a fallback if the network fails.
- Run `python experiments/plots.py` and check the plots are in `experiments/results/`.

## 30 minutes before

Deploy, using the steps in [AWS_STEPS.md](AWS_STEPS.md), so that the instance is already training and
has a few checkpoints:

```bash
python infra/deploy.py --repo-url https://github.com/YOUR-NAME/spotsentry.git --yes
```

```bash
spotsentry dashboard --job jobs/cifar_aws.yaml
```

Have these open: the dashboard, the EC2 console on Spot Requests, and a terminal in the project.

## The script

| Time | Do this | Say this |
|---|---|---|
| 0:00 to 1:00 | Run `spotsentry plan --job jobs/cifar_aws.yaml --offline` | "Before spending money, the planner profiles the job. It predicts run time, picks an instance and zone, sets the checkpoint interval and compares spot with on-demand." |
| 1:00 to 2:00 | Show the dashboard | "CIFAR training is running on a real spot instance. Each mark is a checkpoint. They are captured in milliseconds and uploaded in the background." |
| 2:00 to 4:00 | Run `python scripts/demo_aws.py --interrupt` (the FIS console is not available on this account) | "AWS has just told this machine it has two minutes." Point at: rebalance signal if sent, notice, then the decision line, for example "time left 105 s, full predicted 2 s, so full", then saved. |
| 4:00 to 6:00 | Keep watching the dashboard | "The Auto Scaling group is already starting a replacement. It takes the lease, reads the manifest and restores at exactly the same epoch and step." Point at the two workers on the timeline. |
| 6:00 to 7:00 | Run `python scripts/demo_local.py --scenario local/scenarios/tight_deadline.yaml` | "What if there is not enough time? Here the state is 600 MB, the link is 100 Mbps and only 30 seconds remain. Full cannot fit, so it saves a smaller tier and meets the deadline." |
| 7:00 to 8:00 | Dashboard > Experiments | "Across the experiments: time and cost for each strategy, how often the deadline was met, and final accuracy that matches an uninterrupted run." |

## If something fails

| Problem | Fallback |
|---|---|
| Network or AWS trouble | Play the recording, then run `python scripts/demo_local.py` live |
| Instance not ready in time | Run the local demo first and come back to AWS |
| No replacement yet | Boot takes a few minutes. Carry on with steps 5 and 6, then come back |

## Afterwards

```bash
python infra/teardown.py
```

Do this straight away. Check the EC2 console shows nothing running.

## Likely questions

- **Why not checkpoint more often?** Each checkpoint costs time. Young's interval balances that cost
  against the work lost when an interruption has no warning.
- **Why three tiers?** A smaller checkpoint that finishes beats a full one that does not. The cost of
  light is that optimizer state is lost, which slows training briefly.
- **What if the notice never arrives?** The periodic checkpoints cover that. See experiment E1.
- **Can two instances corrupt the job?** No. The lease uses S3 conditional writes, and a worker that
  loses the lease stops writing.
- **Is the cost saving real?** See `docs/RESULTS.md`. Time is scaled down in the experiments, and the
  dollar figures come from the cost model with AWS price data.
