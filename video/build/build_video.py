"""Compose the Lachesis submission video from one live capture and per-beat narration.

Per-beat audio locked, like the submission kit: each beat's picture is a window of
the capture between two markers, fitted to that beat's measured audio length (the
narration's duration plus a fixed tail, rounded to whole frames). A window longer than
its beat is sped up (waiting compressed, never reordered), capped at MAX_SPEED; a
shorter one holds its last frame. Captions are burned from each beat's captionText,
split by sentence in proportion to its length inside that beat's own audio window, so
they cannot drift from the voice.

A beat can instead be a card: {"card": "tour.txt"} renders that committed text file
as a plain terminal-style card in a monospace font, held for the beat's audio. Line
one is the title; every other line is drawn verbatim, with each verdict word and its
bracketed score picked out in colour. Nothing in a card comes from the capture.

Inputs (all under ROOT):
  capture/production.webm, capture/markers.json   from video/capture.mjs in CI
  media/narration/timing.json and the per-beat mp3   from kit/generate-narration.py
  beats.json   beat id -> [start marker, end marker] (or a number of seconds after start),
               or {"card": "<file>.txt"} for a card beat
  CARD_FONT, CARD_FONT_BOLD   monospace font files for cards (DejaVu Sans Mono by default)
Output: out/lachesis-demo.mp4 and out/receipt.json
"""
from __future__ import annotations

import hashlib
import os
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
FPS = 25
W, H = 1920, 1080
MAX_SPEED = 2.5
MIN_SPEED = 0.6
OUT = ROOT / "out"
SEG = OUT / "seg"

# Card beats. DejaVu Sans Mono advances every glyph 1233/2048 em, so a line's width is
# its length times that. The card stays above the burned captions (CARD_BOTTOM).
DEJAVU = "/usr/share/fonts/truetype/dejavu"
CARD_FONT = os.environ.get("CARD_FONT", f"{DEJAVU}/DejaVuSansMono.ttf")
CARD_FONT_BOLD = os.environ.get("CARD_FONT_BOLD", f"{DEJAVU}/DejaVuSansMono-Bold.ttf")
MONO_ADVANCE = 1233 / 2048
CARD_MARGIN = 96
CARD_BOTTOM = 820
CARD_BG, CARD_PANEL, CARD_RULE = "0x0b0e12", "0x151a21", "0x2b323c"
CARD_TITLE, CARD_TEXT = "0xf2f4f7", "0xaeb6bf"
VERDICT_COLOURS = {"proceed": "0x4ade80", "guide": "0x60a5fa",
                   "confirm": "0xfbbf24", "deny": "0xf87171"}
VERDICT = re.compile(r"\b(proceed|guide|confirm|deny)\b( \([^)]*\))?")


def sh(args: list[str], cwd: pathlib.Path | None = None) -> str:
    r = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(args)[:300]}\n{r.stderr[-2000:]}")
    return r.stdout.strip()


def probe(path: pathlib.Path) -> float:
    return float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]))


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def ref(r: str | float, at: dict[str, float]) -> float:
    """A marker name, a marker with an offset ("finished+4" or "pause3-1"), or seconds."""
    if not isinstance(r, str):
        return float(r)
    m = re.fullmatch(r"([a-z0-9]+)([+-][0-9.]+)?", r)
    if not m or m.group(1) not in at:
        sys.exit(f"unknown marker reference {r!r}")
    return at[m.group(1)] + float(m.group(2) or 0)


def layers(line: str) -> list[tuple[str, str]]:
    """The line as (colour, text) layers of equal length: the plain text with every
    verdict span blanked, then one layer per verdict colour holding only its spans.
    In a monospace font the layers land glyph on glyph, so the text stays verbatim."""
    plain = list(line)
    coloured: dict[str, list[str]] = {}
    for m in VERDICT.finditer(line):
        layer = coloured.setdefault(VERDICT_COLOURS[m.group(1)], [" "] * len(line))
        for k in range(m.start(), m.end()):
            layer[k], plain[k] = line[k], " "
    return [(CARD_TEXT, "".join(plain))] + [(c, "".join(t)) for c, t in coloured.items()]


def render_card(name: str, seg: pathlib.Path, frames: int) -> dict:
    """A terminal-style card from a committed text file: line one is the title, the
    rest are drawn verbatim at the largest size that fits inside the margins."""
    if not re.fullmatch(r"[a-z0-9_-]+\.txt", name):
        sys.exit(f"card {name!r} is not a plain .txt file name")
    lines = [line.rstrip() for line in (ROOT / name).read_text(encoding="utf-8").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) < 2:
        sys.exit(f"card {name} needs a title line and at least one more line")
    for font in (CARD_FONT, CARD_FONT_BOLD):
        if not pathlib.Path(font).is_file():
            sys.exit(f"card font missing: {font} (install fonts-dejavu-core or set CARD_FONT)")
    # Copied beside the segment and named relatively, so no path needs filter escaping.
    shutil.copyfile(CARD_FONT, SEG / "card-mono.ttf")
    shutil.copyfile(CARD_FONT_BOLD, SEG / "card-mono-bold.ttf")
    title, body = lines[0], lines[1:]
    room = W - 2 * CARD_MARGIN
    body_px = min(30, int(room / (max(len(line) for line in body) * MONO_ADVANCE)))
    title_px = min(40, int(room / (len(title) * MONO_ADVANCE)))
    step, pad = round(body_px * 2), 48
    panel_h = pad + title_px + round(title_px * 0.6) + step * len(body) + pad
    top = max(60, (CARD_BOTTOM - panel_h) // 2)
    inset = CARD_MARGIN - 36
    vf = [f"drawbox=x={inset}:y={top}:w={W - 2 * inset}:h={panel_h}:color={CARD_PANEL}:t=fill"]
    base = top + pad + title_px
    rule = base + round(title_px * 0.6)
    vf.append(f"drawbox=x={CARD_MARGIN}:y={rule}:w={room}:h=2:color={CARD_RULE}:t=fill")
    texts = [(f"{seg.stem}-title.txt", title, "card-mono-bold.ttf", title_px, CARD_TITLE, base)]
    for n, line in enumerate(body):
        y = rule + step * (n + 1)
        for k, (colour, text) in enumerate(layers(line)):
            texts.append((f"{seg.stem}-{n}-{k}.txt", text, "card-mono.ttf", body_px, colour, y))
    for file, text, font, px, colour, y in texts:
        (SEG / file).write_text(text, encoding="utf-8")
        vf.append(f"drawtext=fontfile={font}:textfile={file}:expansion=none:fontsize={px}:"
                  f"fontcolor={colour}:x={CARD_MARGIN}:y={y}:y_align=baseline")
    vf.append(f"fps={FPS},format=yuv420p,setsar=1")
    sh(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={CARD_BG}:s={W}x{H}:r={FPS}",
        "-vf", ",".join(vf), "-frames:v", str(frames), "-c:v", "libx264", "-preset", "medium",
        "-crf", "18", "-r", str(FPS), seg.name], cwd=SEG)
    widest = max(len(line) for line in body) * MONO_ADVANCE * body_px
    return {"file": name, "lines": len(body), "font_px": body_px, "title_px": title_px,
            "widest_px": round(widest, 1), "room_px": room, "top": top, "bottom": top + panel_h}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    SEG.mkdir(exist_ok=True)
    markers = json.loads((ROOT / "capture" / "markers.json").read_text(encoding="utf-8"))
    at = {m["id"]: float(m["t"]) for m in markers["markers"]}
    capture_total = float(markers["total"])
    beats = json.loads((ROOT / "beats.json").read_text(encoding="utf-8"))
    timing = json.loads((ROOT / "media" / "narration" / "timing.json").read_text(encoding="utf-8"))
    spec = json.loads((ROOT / "kit" / "narration.json").read_text(encoding="utf-8"))
    captions = {s["id"]: s["captionText"] for s in spec["segments"]}
    scenes = timing["scenes"]
    if [s["id"] for s in scenes] != list(beats):
        sys.exit("beats.json and the narration disagree on the beat order")

    srt, clock, plan, problems = [], 0.0, [], []
    wav_list, seg_list = [], []
    for i, scene in enumerate(scenes):
        sid = scene["id"]
        audio = ROOT / "media" / "narration" / scene["audio"]
        spoken = probe(audio)
        frames = round(float(scene["holdSeconds"]) * FPS)
        hold = frames / FPS
        seg = SEG / f"{i:02d}-{sid}.mp4"
        card = None
        if isinstance(beats[sid], dict):
            # A card beat: rendered text, no capture footage, held for the whole beat.
            a, b = "card", beats[sid]["card"]
            card = render_card(b, seg, frames)
            start, window, speed = 0.0, hold, 1.0
            if card["widest_px"] > card["room_px"]:
                problems.append(f"card {b}: widest line {card['widest_px']} px "
                                f"exceeds {card['room_px']} px")
            if card["font_px"] < 18:
                problems.append(f"card {b}: text at {card['font_px']} px is too small to read")
            if card["bottom"] > CARD_BOTTOM:
                problems.append(f"card {b}: panel reaches y={card['bottom']}, under the captions")
        else:
            a, b = beats[sid]
            start = ref(a, at)
            end = ref(b, at) if isinstance(b, str) else start + float(b)
            end = min(end, capture_total)
            window = max(0.04, end - start)
            # Longer than the beat: compress the waiting, capped. Shorter: play it a little
            # slower, down to MIN_SPEED, then hold the last frame for whatever is left.
            if window > hold:
                speed = min(MAX_SPEED, window / hold)
            else:
                speed = max(MIN_SPEED, window / hold)
            used = min(window, hold * speed)
            vf = (f"trim=start={start:.3f}:duration={used:.3f},setpts=(PTS-STARTPTS)/{speed:.4f},"
                  f"fps={FPS},scale={W}:{H}:flags=lanczos,format=yuv420p,setsar=1,"
                  f"tpad=stop_mode=clone:stop_duration={hold:.3f},trim=end_frame={frames}")
            sh(["ffmpeg", "-y", "-v", "error", "-i", str(ROOT / "capture" / "production.webm"),
                "-an", "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-r", str(FPS), str(seg)])
        wav = SEG / f"{i:02d}-{sid}.wav"
        sh(["ffmpeg", "-y", "-v", "error", "-i", str(audio), "-af", f"apad,atrim=end={hold:.3f}",
            "-ar", "48000", "-ac", "2", str(wav)])
        seg_list.append(seg)
        wav_list.append(wav)
        parts = sentences(captions[sid])
        weights = [len(p) for p in parts]
        t = clock
        for p, wgt in zip(parts, weights):
            d = spoken * wgt / sum(weights)
            srt.append((t, t + d, p))
            t += d
        plan.append({"id": sid, "from": a, "to": b, "start": round(start, 3), "window": round(window, 3),
                     "speed": round(speed, 3), "hold": round(hold, 3), "spoken": round(spoken, 3), "at": round(clock, 3)})
        if card:
            plan[-1]["card"] = card
        clock += hold

    (SEG / "video.txt").write_text("".join(f"file '{p.name}'\n" for p in seg_list), encoding="utf-8")
    (SEG / "audio.txt").write_text("".join(f"file '{p.name}'\n" for p in wav_list), encoding="utf-8")
    sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "video.txt", "-c", "copy", "video.mp4"], cwd=SEG)
    sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "audio.txt", "-c", "copy", "audio.wav"], cwd=SEG)
    with (SEG / "captions.srt").open("w", encoding="utf-8") as f:
        for n, (s, e, text) in enumerate(srt, 1):
            f.write(f"{n}\n{srt_time(s)} --> {srt_time(e)}\n{text}\n\n")
    font = os.environ.get("CAPTION_FONT", "DejaVu Sans")
    style = (f"FontName={font},FontSize=11,PrimaryColour=&H00FFFFFF,BackColour=&H99000000,"
             "BorderStyle=4,Outline=0,Shadow=0,MarginV=22,MarginL=60,MarginR=60")
    final = OUT / "lachesis-demo.mp4"
    sh(["ffmpeg", "-y", "-v", "error", "-i", "video.mp4", "-i", "audio.wav",
        "-vf", f"subtitles=captions.srt:force_style='{style}'",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart", "-shortest", str(final)], cwd=SEG)

    vdur = float(sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(final)]))
    adur = float(sh(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(final)]))
    dims = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(final)])
    total = round(clock, 3)
    if abs(vdur - total) > 1.5 / FPS:
        problems.append(f"video {vdur:.3f}s != narration timeline {total:.3f}s")
    if abs(vdur - adur) > 1.5 / FPS:
        problems.append(f"video {vdur:.3f}s and audio {adur:.3f}s differ")
    if not 90 <= vdur < 180:
        problems.append(f"duration {vdur:.1f}s outside 90 to 180")
    if dims != f"{W},{H}":
        problems.append(f"dimensions {dims}")
    receipt = {"file": final.name, "duration": round(vdur, 3), "audio": round(adur, 3), "timeline": total,
               "dimensions": dims, "fps": FPS, "beats": plan, "captions": len(srt),
               "sha256": hashlib.sha256(final.read_bytes()).hexdigest(), "problems": problems,
               "capture_site": markers.get("site"), "capture_errors": markers.get("errors", [])}
    (OUT / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    frames = OUT / "frames"
    frames.mkdir(exist_ok=True)
    for p in plan:
        mid = p["at"] + p["hold"] / 2
        sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{mid:.2f}", "-i", str(final), "-frames:v", "1",
            "-vf", "scale=960:-2", "-q:v", "4", str(frames / f"{p['id']}.jpg")])
        if "card" in p:
            # Card text is small at 960 px, so a card also gets a full-size frame to read.
            sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{mid:.2f}", "-i", str(final), "-frames:v", "1",
                "-q:v", "2", str(frames / f"{p['id']}-{W}.jpg")])
    print(json.dumps({k: receipt[k] for k in ("duration", "audio", "timeline", "dimensions", "captions", "problems")}))
    for p in plan:
        print(p)
    if problems:
        sys.exit("video checks failed: " + "; ".join(problems))


if __name__ == "__main__":
    main()
