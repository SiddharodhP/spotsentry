# Running on real AWS

This creates resources that cost money. Nothing is created until you pass `--yes`. Expect a few
cents per hour while one instance runs. The CloudFormation template has been linted, but **it has not
been deployed yet**, so treat the first deploy as a test and watch it.

## Before you start

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
   python infra/deploy.py --repo-url https://github.com/YOUR-NAME/spotsentry.git
   ```

2. Deploy:

   ```bash
   python infra/deploy.py --repo-url https://github.com/YOUR-NAME/spotsentry.git --yes
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

## Interrupt it for real

Wait until a few checkpoints appear in the dashboard, then do either of these:

- **From the console, for the live demo:** EC2 > Spot Requests > select the request > Actions >
  Initiate interruption.
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
   spotsentry plan --job jobs/cifar_aws.yaml --candidates c5.large,m5.large,c6i.large --regions ap-south-1
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
in AWS FIS (experiment templates) is free, and you can delete it from the FIS console.

## If something goes wrong

| What you see | What to do |
|---|---|
| Stack fails creating the bucket | The name is taken. Pass `--bucket some-other-name` |
| Spot request stays pending | No capacity at this price. Try other `--instance-types` |
| Instance boots but no events | Read `/var/log/spotsentry.log`. Usually the repository URL or a private repository |
| "Initiate interruption" missing or fails | The account is on the Free plan, or the region does not support it |
| Costs are climbing | Run `python infra/teardown.py` right away |

## Booting faster (optional)

Boot time counts towards recovery time. To cut it: let one instance finish its first boot, create an
image of it (EC2 > Actions > Image and templates > Create image), store the image ID in an SSM
parameter, and pass that parameter as `ImageId` in the stack. The bootstrap script skips installing
when it finds the project already there, but the image then also holds that version of the code.
