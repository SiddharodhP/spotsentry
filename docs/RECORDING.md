# Recording the AWS demo

A backup video of a real run: a spot instance in Sydney trains CIFAR-10, AWS sends a real
interruption, SpotSentry saves in under a second, and a new instance carries on from the same step.
Everything on screen is live. Nothing is staged or edited except cutting out waiting time.

Total time: about 45 minutes, including about 30 minutes of recording. Cost: a few cents.

## What makes it convincing

The same facts appear in three independent places at once. Arrange the screen so all three are
visible:

| Place | Shows |
|---|---|
| **AWS console** (browser, left half) | The instance ID, "spot" lifecycle, Sydney region, the interruption experiment |
| **Dashboard** (browser, right half) | State, epoch and step ticking, checkpoints, the notice, the emergency save, the restore |
| **Terminal** (bottom) | The raw event stream from S3, with instance IDs and times |

The instance IDs match across all three, and the times match the taskbar clock.

## The day before

1. **Install OBS Studio** (free, records the whole screen):

   ```bash
   winget install OBSProject.OBSStudio
   ```

   In OBS: Sources > + > Display Capture > your screen. Settings > Output > Recording format: mp4.
   Settings > Video: 1920x1080, 30 fps. If you will narrate, check the microphone level in the mixer.
   The Snipping Tool (Win+Shift+R) also works, but has no microphone or pause.

2. **Turn on Do Not Disturb** in Windows, so no notifications pop up.

3. **Prepare the AWS console tabs**, signed in, region **Asia Pacific (Sydney)** (top right):
   - EC2 > Instances. Filter: `tag:spotsentry = true`. Gear icon: turn on the **Instance lifecycle**
     and **Availability zone** columns.
   - EC2 > Spot Requests.
   - AWS FIS > Experiments.
   - S3 > `spotsentry-662182610728-ap-southeast-2` > `jobs/` > `cifar-aws/` > `checkpoints/`
     (it fills up during the run).

4. **Check nothing is running:**

   ```bash
   python scripts/demo_aws.py --no-follow
   ```

   It should say "No running instance tagged spotsentry=true."

## Recording

Open two terminals in `D:\CCel` with the virtual environment active.

1. **Start the dashboard** in terminal 1, and put the browser tab on the right half of the screen:

   ```bash
   spotsentry dashboard --job jobs/cifar_aws.yaml
   ```

2. **Start recording** in OBS.

3. **Start a fresh run** in terminal 2:

   ```bash
   python scripts/demo_aws.py --start
   ```

   Say: "This clears the previous run and asks the Auto Scaling group for one spot instance."
   Leave this terminal running. It prints every event as it happens.

4. **Show the instance appear**, about 1 minute. On EC2 > Instances, click refresh. A new instance
   shows **Pending** then **Running**, Instance lifecycle **spot**, zone `ap-southeast-2…`. On Spot
   Requests, its request is **active / fulfilled**.

5. **Show training start**, about 1 more minute. In the dashboard, the state becomes **running**,
   and the instance ID matches the console. Epoch and step count up, and a checkpoint is saved
   roughly every 30 seconds. In S3, refresh `checkpoints/` to show files arriving.

6. **Wait about 3 minutes**, so several checkpoints exist. You can pause OBS here.

7. **Send the real interruption.** EC2 > Spot Requests > select the active request > **Actions >
   Initiate interruption**. If it asks for an IAM role, choose the one starting
   `spotsentry-FisRole-`. Confirm.

   If the console option is missing or fails, run this in a third terminal instead. It does the
   same thing through AWS FIS:

   ```bash
   python scripts/demo_aws.py --interrupt
   ```

   Say: "AWS has just told this machine it will be taken away in two minutes."

8. **Watch the notice and the save**, within a few seconds. Point at each in the dashboard timeline
   and the terminal:
   - **Interruption notice**: the deadline AWS gave (printed in UTC, ending in `Z`)
   - **Emergency decision**: time left, the predicted time for each checkpoint size, the size chosen
   - **Emergency saved**: saved in a fraction of a second, well inside the deadline
   - the worker exits with code 75

   In AWS FIS > Experiments, the experiment shows **running** and later **completed**.

9. **Watch the replacement**, about 80 seconds. On EC2 > Instances, refresh: the old instance goes
   to **Shutting-down / Terminated**, and a new instance appears. In the dashboard: **Restored** at
   the same epoch and step the emergency save recorded, a **new instance ID**, and **Restarts: 1**.
   Training continues.

10. **Optional, a second interruption** after another 2 to 3 minutes. Repeat steps 7 to 9.

11. **Show the finish**, or stop here. The job finishes about 25 minutes after it started: the
    dashboard shows **completed** with the test accuracy (80.71% in earlier runs), and the instance
    terminates by itself because the job scales the group to zero. Pause OBS during the wait and
    resume a minute before the end, or record it all and cut later.

12. **Stop recording.** Then check that nothing is running:

    ```bash
    python scripts/demo_aws.py --no-follow
    ```

## Editing

- Cut or speed up waiting, and put a caption on sped-up parts such as "8x speed". Keep the taskbar
  clock visible, so viewers can see real time passing.
- Never change a value on screen. The point of the video is that it is real.
- A good final length is 5 to 8 minutes: start, training, interruption, save, replacement, restore,
  finish.

## Things to keep off screen

- The access key file and `C:\Users\siddh\.aws\`. Never run `aws configure` while recording.
- Your email in the console's top-right menu. Do not open that menu.
- The account ID is in the bucket name. That is safe to show, but you can blur it.

## If something goes wrong

| What you see | What to do |
|---|---|
| `--start` says an instance is already running | Wait for that run to finish, or run `python infra/teardown.py` and redeploy |
| No instance after 3 minutes | EC2 > Auto Scaling groups > `spotsentry-asg` > Activity shows the reason |
| Dashboard says it cannot read the store | Check `aws sts get-caller-identity` works, then restart the dashboard |
| "Initiate interruption" is missing | Use `python scripts/demo_aws.py --interrupt` |
