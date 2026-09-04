"""Render the animated terminal demo GIF shown in the README.

Simulates the bot's live terminal output: messages appearing, the trigger
firing, and a reply being sent. Usernames are fictional.
"""

from PIL import Image, ImageDraw, ImageFont

W, H = 900, 580
PAD_X, PAD_Y = 26, 58
LINE_H = 26
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG = (11, 9, 16)
CHROME = (25, 21, 39)
BORDER = (47, 42, 61)
DIM = (107, 100, 120)
TEXT = (214, 211, 209)
CYAN = (125, 211, 192)
ORANGE = (217, 119, 87)
GREEN = (97, 197, 84)
YELLOW = (245, 189, 79)
FAINT = (77, 71, 89)

font = ImageFont.truetype(FONT_PATH, 15)
font_bold = ImageFont.truetype(FONT_BOLD, 15)
font_title = ImageFont.truetype(FONT_PATH, 13)

# Each line: list of (text, color, bold) segments
SCRIPT = [
    ("boot", [("Watching thread 3402823668...  (Ctrl+C to stop)", DIM, False)]),
    ("blank", []),
    ("type", [("[19:14] ", DIM, False), ("mara:      ", CYAN, False),
              ("computah settle this, is a hotdog a sandwich", TEXT, False)]),
    ("instant", [("  [computah triggered]", YELLOW, False)]),
    ("wait", []),
    ("instant", [("  -> replied to 1 message(s): structurally yes, spiritually no,", DIM, False)]),
    ("instant", [("     and you already knew that before you asked", DIM, False)]),
    ("instant", [("[19:14] ", DIM, False), ("computah:  ", ORANGE, True),
                 ("@mara structurally yes, spiritually no, and you", TEXT, False)]),
    ("instant", [("                   already knew that before you asked", TEXT, False)]),
    ("blank", []),
    ("type", [("[19:15] ", DIM, False), ("dev_kel:   ", CYAN, False),
             ("computah who is the worst person here", TEXT, False)]),
    ("type", [("[19:15] ", DIM, False), ("tomas:     ", CYAN, False),
             ("computah back me up, mara is wrong", TEXT, False)]),
    ("instant", [("  [computah triggered]", YELLOW, False)]),
    ("wait", []),
    ("instant", [("  -> replied to 2 message(s): kel it's whoever asked, and", DIM, False)]),
    ("instant", [("     tomas i'm not dying on that hill for you", DIM, False)]),
    ("instant", [("[19:15] ", DIM, False), ("computah:  ", ORANGE, True),
                 ("@dev_kel @tomas it's whoever asked, and tomas", TEXT, False)]),
    ("instant", [("                   i'm not dying on that hill for you", TEXT, False)]),
    ("blank", []),
    ("instant", [("  -> updated persona for tomas", GREEN, False)]),
]


def new_canvas():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # window chrome
    d.rectangle([0, 0, W - 1, 38], fill=CHROME)
    d.rectangle([0, 0, W - 1, H - 1], outline=BORDER)
    for i, c in enumerate([(239, 107, 94), (245, 189, 79), (97, 197, 84)]):
        d.ellipse([20 + i * 20, 14, 30 + i * 20, 24], fill=c)
    d.text((W // 2 - 42, 12), "bot.py", font=font_title, fill=DIM)
    return img, d


def draw_lines(d, lines, cursor_on=False):
    """lines: list of list-of-segments already resolved to visible text."""
    y = PAD_Y
    for segs in lines:
        x = PAD_X
        for text, color, bold in segs:
            f = font_bold if bold else font
            d.text((x, y), text, font=f, fill=color)
            x += d.textlength(text, font=f)
        if cursor_on and segs is lines[-1]:
            d.rectangle([x + 2, y + 2, x + 9, y + 17], fill=TEXT)
        y += LINE_H


def visible_prefix(segs, chars):
    """Return segments truncated to the first `chars` visible characters."""
    out, used = [], 0
    for text, color, bold in segs:
        if used >= chars:
            break
        take = min(len(text), chars - used)
        out.append((text[:take], color, bold))
        used += take
    return out


frames, durations = [], []
rendered = []  # completed lines

for kind, segs in SCRIPT:
    if kind == "blank":
        rendered.append([])
        img, d = new_canvas()
        draw_lines(d, rendered[-20:])
        frames.append(img)
        durations.append(90)

    elif kind == "boot":
        img, d = new_canvas()
        rendered.append(segs)
        draw_lines(d, rendered[-20:])
        frames.append(img)
        durations.append(700)

    elif kind == "instant":
        rendered.append(segs)
        img, d = new_canvas()
        draw_lines(d, rendered[-20:])
        frames.append(img)
        durations.append(420)

    elif kind == "wait":
        # thinking pause with a blinking cursor
        for on in (True, False, True):
            img, d = new_canvas()
            draw_lines(d, rendered[-20:], cursor_on=on)
            frames.append(img)
            durations.append(260)

    elif kind == "type":
        total = sum(len(t) for t, _, _ in segs)
        rendered.append([])
        step = 3
        for n in range(0, total + 1, step):
            rendered[-1] = visible_prefix(segs, n)
            img, d = new_canvas()
            draw_lines(d, rendered[-20:], cursor_on=(n < total))
            frames.append(img)
            durations.append(28)
        rendered[-1] = segs

# hold the final frame
img, d = new_canvas()
draw_lines(d, rendered[-20:])
frames.append(img)
durations.append(2600)

frames[0].save(
    "docs/demo.gif",
    save_all=True,
    append_images=frames[1:],
    duration=durations,
    loop=0,
    optimize=True,
)
print(f"frames: {len(frames)}  total: {sum(durations)/1000:.1f}s")
