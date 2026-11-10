# Running on real AWS

This creates resources that cost money. Nothing is created until you pass `--yes`. Expect a few
cents per hour while one instance runs. The template was first deployed on 2026-11-07, to Sydney on the
Free plan, and ran a full CIFAR-10 job (see `docs/NOTES.md`).

## Before you start

Run every command below from the project folder (`D:\CCel` on the team laptop) with the virtual
environment active: the prompt must start with `(.venv)`. Do this in every new terminal; see the
README's [Every new terminal](../README.md#every-new-terminal).

0. **Region: Sydney (`ap-southeast-2`).** The team's account only allows that region (see
   `docs/NOTES.md`), so it is the default for `deploy.py`, `teardown.py`, `spotsentry plan` and
   `jobs/cifar_aws.yaml`. For an account without that limit, pass `--region` to `deploy.py`.
1. **Use the Paid plan.** Initiating a spot interruption uses AWS Fault Injection Service, which the
   Free plan does not include. In the console, check Billing > Account plan. Credits still apply.
2. **Set a budget.** Billing > Budgets > Create budget > Monthly cost budget, $5, with an email alert.
3. **Install the AWS CLI** and sign in. Create an IAM user or SSO profile for yourself rather than
   using the root account. Never put access keys in the project.

   ```bash
   winget install Amazon.AWSCLI
   ```

   ```bash
   aws configure
   ```

   ```bash
   aws sts get-caller-identity
   ```

4. **Put the project on GitHub.** Instances clone it when they boot, so the repository must be public
   or the URL must work without a password. Check that `.env` is not committed.

   ```bash
   git init
   ```

   ```bash
   git add .
   ```

   ```bash
   git status
   ```

   ```bash
   git commit -m "SpotSentry"
   ```

   Then create the GitHub repository and push to it.

## Deploy

1. See the plan and estimated cost. This creates nothing:

   ```bash
   python infra/deploy.py --repo-url https://github.com/SiddharodhP/spotsentry.git
   ```

2. Deploy:

   ```bash
   python infra/deploy.py --repo-url https://github.com/SiddharodhP/spotsentry.git --yes
   ```

3. Watch it. The first boot takes a few minutes, because it installs Python and PyTorch and downloads
   CIFAR-10:

   ```bash
   python scripts/demo_aws.py
   ```

   If no events appear after about 10 minutes, look at the instance's log. In the EC2 console choose the
   instance > Connect > Session Manager, then run `sudo tail -50 /var/log/spotsentry.log`.

4. In a second terminal, open the dashboard:

   ```bash
   spotsentry dashboard --job jobs/cifar_aws.yaml
   ```

## Run it again

A finished job stays marked as completed, so a new instance would exit at once. To start over
at epoch 0 on the stack you already have, without redeploying:

```bash
python scripts/demo_aws.py --start
```

It deletes the job's previous status, lease, events and checkpoints from the bucket (it keeps
`plan.json` and the CIFAR-10 copy), asks the Auto Scaling group for one instance, then follows the
events. It refuses if an instance is already running. Training starts about 2 minutes after
launch, and the job scales back to zero when it finishes.

## Interrupt it for real

Wait until a few checkpoints appear in the dashboard, then do either of these:

- **From the console:** EC2 > Spot Requests > select the request > Actions > Initiate interruption.
  On the team's account ("AWS (new)" console experience) the FIS console is not available, so use
  the command line below. The FIS API itself works.
- **From the command line:**

  ```bash
  python scripts/demo_aws.py --interrupt
  ```

You should see the interruption notice, an emergency decision, `emergency_saved`, and then a new
instance acquiring the lease and restoring at the same epoch and step.

## Things to measure while it is up

These replace placeholders in the results. Do them before tearing down.

1. **Relative speed of instance types.** The planner's `relative_speed.yaml` holds placeholder numbers.
   On each type, run this and put the speed ratios into `spotsentry/planner/data/relative_speed.yaml`:

   ```bash
   spotsentry profile --job jobs/cifar_aws.yaml
   ```

2. **The online planner**, with your AWS credentials:

   ```bash
   spotsentry plan --job jobs/cifar_aws.yaml --candidates c7i-flex.large,m7i-flex.large,t3.small --regions ap-southeast-2
   ```

3. **Record the screen** during a complete successful run. That is your backup for the live review.

## Tear down

Do this every time you finish. It stops the charges.

```bash
python infra/teardown.py
```

It sets the group to zero, asks before emptying the bucket, deletes the stack and prints a checklist.
Then check by eye: the EC2 console's instance list should show nothing running, and the
CloudFormation console should show no `spotsentry` stack. Anything created by `demo_aws.py --interrupt`
in AWS FIS (experiment templates) is free. On an account without the FIS console, list them with
`aws fis list-experiment-templates` and remove each with `aws fis delete-experiment-template --id ID`.

## If something goes wrong

| What you see | What to do |
|---|---|
| Stack fails creating the bucket | The name is taken. Pass `--bucket some-other-name` |
| Spot request stays pending | No capacity at this price. Try other `--instance-types` |
| Instance boots but no events | Read `/var/log/spotsentry.log`. Usually the repository URL or a private repository |
| "Initiate interruption" missing or fails | The account is on the Free plan, or uses the "AWS (new)" console without FIS pages. Use `python scripts/demo_aws.py --interrupt` |
| Costs are climbing | Run `python infra/teardown.py` right away |

## Booting faster (optional)

Boot time counts towards recovery time. To cut it: let one instance finish its first boot, create an
image of it (EC2 > Actions > Image and templates > Create image), store the image ID in an SSM
parameter, and pass that parameter as `ImageId` in the stack. The bootstrap script skips installing
when it finds the project already there, but the image then also holds that version of the code.
