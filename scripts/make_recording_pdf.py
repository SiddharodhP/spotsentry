"""Phone-sized PDF of docs/RECORDING.md. Needs Windows fonts (Segoe UI, Consolas).

    python scripts/make_recording_pdf.py docs/RECORDING_phone.pdf

The content is written out here, so update it when docs/RECORDING.md changes.
"""
import sys

from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

OUT = sys.argv[1]
F = "C:/Windows/Fonts/"
pdfmetrics.registerFont(TTFont("UI", F + "segoeui.ttf"))
pdfmetrics.registerFont(TTFont("UI-B", F + "segoeuib.ttf"))
pdfmetrics.registerFont(TTFont("UI-I", F + "segoeuii.ttf"))
pdfmetrics.registerFont(TTFont("UI-SB", F + "seguisb.ttf"))
pdfmetrics.registerFont(TTFont("Mono", F + "consola.ttf"))
pdfmetrics.registerFont(TTFont("Mono-B", F + "consolab.ttf"))
pdfmetrics.registerFontFamily("UI", normal="UI", bold="UI-B", italic="UI-I", boldItalic="UI-B")

W, H = 100 * mm, 178 * mm  # 9:16, a phone screen
M = 6 * mm
CW = W - 2 * M

INK = HexColor("#1b1f24")
MUTED = HexColor("#5b6470")
ACCENT = HexColor("#1f5fbf")
CARD = HexColor("#f4f6f9")
CMD_BG = HexColor("#1e2430")
CMD_FG = HexColor("#e8edf5")
SAY_BG = HexColor("#fff6e0")
SAY_BAR = HexColor("#e0a100")
WARN_BG = HexColor("#fdecec")
WARN_BAR = HexColor("#c93a3a")
LINE = HexColor("#d8dde4")

base = dict(fontName="UI", textColor=INK, fontSize=10.5, leading=14.5)
S = {
    "title": ParagraphStyle("title", **{**base, "fontName": "UI-B", "fontSize": 19, "leading": 23}),
    "sub": ParagraphStyle("sub", **{**base, "textColor": MUTED, "fontSize": 10, "leading": 14}),
    "h": ParagraphStyle("h", **{**base, "fontName": "UI-B", "fontSize": 14, "leading": 18,
                                "textColor": ACCENT, "spaceBefore": 2, "spaceAfter": 4}),
    "step": ParagraphStyle("step", **{**base, "fontName": "UI-B", "fontSize": 12.5, "leading": 16}),
    "body": ParagraphStyle("body", **base),
    "small": ParagraphStyle("small", **{**base, "fontSize": 9, "leading": 12.5,
                                        "textColor": MUTED}),
    "bullet": ParagraphStyle("bullet", **{**base, "leftIndent": 9, "bulletIndent": 0}),
    "cmd": ParagraphStyle("cmd", fontName="Mono", fontSize=8.6, leading=11.5,
                          textColor=CMD_FG),
    "say": ParagraphStyle("say", **{**base, "fontName": "UI-I"}),
    "num": ParagraphStyle("num", fontName="UI-B", fontSize=13, leading=15, textColor=white,
                          alignment=1),
    "time": ParagraphStyle("time", fontName="UI-SB", fontSize=8.5, leading=11, textColor=ACCENT,
                           alignment=2),
}


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def box(flow, bg, bar=None, pad=5):
    t = Table([[flow]], colWidths=[CW - (0 if bar is None else 0)])
    style = [
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), pad + (2 if bar else 0)),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
    ]
    if bar:
        style.append(("LINEBEFORE", (0, 0), (0, -1), 3, bar))
    t.setStyle(TableStyle(style))
    return t


def cmd(text):
    return box(Paragraph(esc(text).replace(" ", "&nbsp;", 0), S["cmd"]), CMD_BG, pad=6)


def say(text):
    return box(Paragraph(f"<b>Say:</b> \u201c{text}\u201d", S["say"]), SAY_BG, SAY_BAR)


def warn(text):
    return box(Paragraph(text, S["body"]), WARN_BG, WARN_BAR)


def bullets(items):
    return [Paragraph(i, S["bullet"], bulletText="\u2022") for i in items]


def step(n, title, when, parts):
    """A card: number badge, title, time on the right, then its contents."""
    label = str(n)
    num_style = S["num"] if len(label) == 1 else ParagraphStyle(
        "num2", parent=S["num"], fontSize=10.5, leading=12)
    badge = Table([[Paragraph(label, num_style)]], colWidths=[9 * mm], rowHeights=[8 * mm])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    head = Table(
        [[badge, Paragraph(title, S["step"]), Paragraph(when, S["time"])]],
        colWidths=[11 * mm, CW - 11 * mm - 20 * mm, 20 * mm],
    )
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flows = [head]
    for p in parts:
        flows.append(Spacer(1, 3))
        flows.append(Paragraph(p, S["body"]) if isinstance(p, str) else p)
    flows.append(Spacer(1, 4))
    flows.append(Table([[""]], colWidths=[CW], rowHeights=[0.6],
                       style=[("LINEABOVE", (0, 0), (-1, -1), 0.6, LINE)]))
    flows.append(Spacer(1, 6))
    return KeepTogether(flows)


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("UI", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(M, 3.5 * mm, "SpotSentry \u00b7 recording guide")
    canvas.drawRightString(W - M, 3.5 * mm, f"{doc.page}")
    canvas.restoreState()


story = []
P = lambda t, s="body": Paragraph(t, S[s])  # noqa: E731

# Page 1: what and why
story += [
    P("Recording the AWS demo", "title"),
    Spacer(1, 3),
    P("A real spot instance in Sydney trains CIFAR-10. AWS sends a real interruption. "
      "SpotSentry saves in under a second, and a new instance carries on from the same step.",
      "sub"),
    Spacer(1, 6),
    P("About 45 min in total, 30 min of it recording. Cost: a few cents.", "body"),
    Spacer(1, 10),
    P("Screen layout", "h"),
    P("The same facts in three places at once. That is what makes it convincing."),
    Spacer(1, 4),
]
layout = Table(
    [[P("<b>Left</b>"), P("AWS console: instance ID, <b>spot</b>, Sydney")],
     [P("<b>Right</b>"), P("Dashboard: epoch/step, checkpoints, notice, save, restore")],
     [P("<b>Bottom</b>"), P("Terminal: live events from S3, with instance IDs and times")]],
    colWidths=[17 * mm, CW - 17 * mm],
)
layout.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, -1), CARD),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LINEBELOW", (0, 0), (-1, -2), 0.6, white),
    ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
]))
story += [layout, Spacer(1, 6),
          P("Instance IDs match across all three. Times match the taskbar clock.", "small"),
          Spacer(1, 10), P("Keep off screen", "h")]
story += bullets([
    "The <b>.aws</b> folder and the access key file",
    "Never run <b>aws configure</b>",
    "The console's top-right account menu (shows your email)",
])
story.append(PageBreak())

# Page 2+: the day before
story.append(P("The day before", "h"))
story.append(step(1, "Recorder ready", "", [
    "<b>OBS Studio</b> (has pause and microphone):",
    cmd("winget install OBSProject.OBSStudio"),
    "Sources > + > Display Capture. Output: mp4. Video: 1920x1080, 30 fps.",
    P("Or Snipping Tool: <b>Win+Shift+R</b>, select the whole screen (no pause).", "small"),
]))
story.append(step(2, "Do Not Disturb on", "", ["So no notifications appear on the video."]))
story.append(step(3, "AWS console tabs", "", [
    "Region <b>Asia Pacific (Sydney)</b>, top right.",
] + bullets([
    "<b>EC2 > Instances</b>. Filter <b>tag:spotsentry = true</b>. Gear: turn on "
    "<b>Instance lifecycle</b> and <b>Availability zone</b>.",
    "<b>EC2 > Spot Requests</b>",
    "<b>S3</b> > bucket > jobs/ > cifar-aws/ > checkpoints/",
])))
story.append(PageBreak())

# Every terminal
story.append(P("Before you press record", "h"))
story.append(step("T", "Every terminal, all three", "", [
    "Do this in <b>each</b> terminal:",
    cmd(r"cd D:\CCel"),
    cmd(r".venv\Scripts\Activate.ps1"),
    "The prompt must start with:",
    cmd(r"(.venv) PS D:\CCel>"),
    "Check, it must print <b>ready</b>:",
    cmd("python -c \"import boto3, spotsentry; print('ready')\""),
    P("1 = dashboard, 2 = event stream, 3 = interrupt and FIS check.", "small"),
]))
story.append(step("0", "Nothing running", "", [
    "In terminal 2:",
    cmd("python scripts/demo_aws.py --no-follow"),
    "Must say: <b>No running instance tagged spotsentry=true.</b>",
]))
story.append(PageBreak())

# Recording
story.append(P("Recording", "h"))
story.append(step(1, "Dashboard", "terminal 1", [
    cmd("spotsentry dashboard --job jobs/cifar_aws.yaml"),
    "Put the browser tab on the right half.",
]))
story.append(step(2, "Start recording", "", ["OBS: <b>Start Recording</b>."]))
story.append(step(3, "Fresh run", "terminal 2", [
    cmd("python scripts/demo_aws.py --start"),
    say("This clears the previous run and asks the Auto Scaling group for one spot instance."),
    "Leave it running. It prints every event.",
]))
story.append(step(4, "Instance appears", "~1 min", [
    "<b>EC2 > Instances</b>, refresh:",
] + bullets([
    "<b>Pending</b> then <b>Running</b>",
    "Lifecycle <b>spot</b>, zone ap-southeast-2…",
    "Spot Requests: <b>active / fulfilled</b>",
])))
story.append(step(5, "Training starts", "~1 min", bullets([
    "Dashboard state <b>running</b>",
    "Same instance ID as the console",
    "Epoch and step counting up",
    "A checkpoint about every 30 s",
    "S3 checkpoints/: refresh, files arrive",
])))
story.append(step(6, "Let checkpoints build", "~3 min", [
    "You can pause OBS here.",
]))
story.append(step(7, "Real interruption", "terminal 3", [
    cmd("python scripts/demo_aws.py --interrupt"),
    say("AWS has just told this machine it will be taken away in two minutes."),
    P("Use the terminal, not the console: this account has no FIS console pages.", "small"),
]))
story.append(step(8, "Notice and save", "seconds", [
    "Point at each in the dashboard and terminal:",
] + bullets([
    "<b>Interruption notice</b>: AWS's deadline (UTC, ends in Z)",
    "<b>Emergency decision</b>: time left, predicted times, size chosen",
    "<b>Emergency saved</b>: well under a second",
    "Worker exits with code <b>75</b>",
]) + [
    "Proof the FIS experiment ran (terminal 3):",
    cmd("aws fis list-experiments --region ap-southeast-2"),
    P("Shows running, later completed.", "small"),
]))
story.append(step(9, "Replacement", "~80 s", bullets([
    "Console: old instance <b>Terminated</b>, a new one appears",
    "Dashboard: <b>Restored</b> at the same epoch and step",
    "New instance ID, <b>Restarts: 1</b>",
    "Training continues",
])))
story.append(step(10, "Optional: again", "+2-3 min", ["Repeat steps 7 to 9."]))
story.append(step(11, "Finish", "~25 min in", [
    "Dashboard: <b>completed</b>, accuracy about <b>80.7%</b>. The instance shuts itself down.",
    P("Pause OBS during the wait, resume a minute before the end.", "small"),
]))
story.append(step(12, "Stop and check", "", [
    "OBS: <b>Stop Recording</b>. Then:",
    cmd("python scripts/demo_aws.py --no-follow"),
    "Must say no running instance.",
]))
story.append(PageBreak())

# Editing and trouble
story.append(P("Editing", "h"))
story += bullets([
    "Cut or speed up waiting. Caption it, e.g. <b>8x speed</b>.",
    "Keep the taskbar clock visible.",
    "<b>Never change a value on screen.</b>",
    "Final length: 5 to 8 minutes.",
])
story += [Spacer(1, 10), P("If something goes wrong", "h")]
trouble = [
    ("<b>No module named boto3</b>, or <b>spotsentry</b> not recognized",
     "Not activated: <font name='Mono'>.venv\\Scripts\\Activate.ps1</font>"),
    ("<b>can't open file</b> scripts\\…", "Wrong folder: <font name='Mono'>cd D:\\CCel</font>"),
    ("<b>--start</b>: instance already running", "Wait for that run to finish"),
    ("No instance after 3 min", "EC2 > Auto Scaling groups > spotsentry-asg > Activity"),
    ("Dashboard can't read the store",
     "Check <font name='Mono'>aws sts get-caller-identity</font>, restart the dashboard"),
]
for problem, fix in trouble:
    story.append(KeepTogether([
        box(Paragraph(problem, S["body"]), CARD, ACCENT),
        Spacer(1, 2),
        Paragraph(fix, S["small"]),
        Spacer(1, 6),
    ]))

doc = BaseDocTemplate(OUT, pagesize=(W, H), leftMargin=M, rightMargin=M, topMargin=M,
                      bottomMargin=8 * mm, title="SpotSentry recording guide",
                      author="SpotSentry team")
doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(M, 8 * mm, CW, H - M - 8 * mm, id="f",
                                                         leftPadding=0, rightPadding=0,
                                                         topPadding=0, bottomPadding=0)],
                                   onPage=footer)])
doc.build(story)
print("wrote", OUT)
