#!/usr/bin/env python3
"""Burn one-word-at-a-time captions onto a video.

Style: white text, black outline, yellow box, bottom of frame, pop-scale animation.
Transcription runs locally via whisper.cpp -- no network, no API.
"""
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WHISPER_BIN = HERE / "whisper.cpp" / "build" / "bin" / "whisper-cli"
MODEL_DIR = HERE / "whisper.cpp" / "models"



def load_vocab(path):
    """vocab.txt -> a priming prompt so Whisper expects your recurring terms."""
    if not path or not os.path.exists(path):
        return ""
    terms = [l.strip() for l in open(path) if l.strip() and not l.startswith("#")]
    return ", ".join(terms)


def load_fixes(path):
    """fixes.txt lines of 'wrong => right' for terms Whisper still gets wrong."""
    out = []
    if not path or not os.path.exists(path):
        return out
    for l in open(path):
        l = l.strip()
        if not l or l.startswith("#") or "=>" not in l:
            continue
        a, b = l.split("=>", 1)
        if a.strip():
            out.append((a.strip(), b.strip()))
    return out


def apply_fixes(words, fixes):
    """Single-word and equal-length multi-word replacements over the word list."""
    single = {}
    phrases = []
    for wrong, right in fixes:
        wl, rl = wrong.split(), right.split()
        if len(wl) == 1 and len(rl) == 1:
            single[wl[0].lower()] = rl[0]
        elif len(wl) == len(rl):
            phrases.append((([x.lower() for x in wl]), rl))
        else:
            print(f"[warn] skipping '{wrong} => {right}' (word counts differ)")

    def bare(t):
        i = len(t)
        while i and t[i - 1] in ".,!?":
            i -= 1
        return t[:i], t[i:]

    for i, w in enumerate(words):
        for wl, rl in phrases:
            if i + len(wl) > len(words):
                continue
            if all(bare(words[i + k]["text"])[0].lower() == wl[k] for k in range(len(wl))):
                for k in range(len(wl)):
                    words[i + k]["text"] = rl[k] + bare(words[i + k]["text"])[1]

    for w in words:
        core, punct = bare(w["text"])
        if core.lower() in single:
            w["text"] = single[core.lower()] + punct
    return words


def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "json", video],
                         capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def extract_audio(video, wav):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video,
                    "-vn", "-ac", "1", "-ar", "16000", wav], check=True)


def transcribe(wav, whisper_bin, model, lang, dtw, prompt="", carry=False,
               progress_cb=None):
    """Run whisper.cpp with -ml 1 -sow so each segment is a single word."""
    stem = str(Path(wav).with_suffix(""))
    cmd = [str(whisper_bin), "-m", str(model), "-f", wav,
           "-ml", "1", "-sow", "-oj", "-of", stem,
           "-t", str(os.cpu_count() or 4), "-pp"]
    if lang:
        cmd += ["-l", lang]
    if dtw:
        cmd += ["--dtw", dtw]
    if prompt:
        cmd += ["--prompt", prompt]
        if carry:
            cmd += ["--carry-initial-prompt"]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    pat = re.compile(r"progress\s*=\s*(\d+)%")
    for line in proc.stderr:
        m = pat.search(line)
        if m and progress_cb:
            progress_cb(int(m.group(1)))
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"whisper-cli failed (exit {proc.returncode})")

    data = json.load(open(stem + ".json"))
    words = []
    for seg in data.get("transcription", []):
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        off = seg.get("offsets", {})
        words.append({"start": round(off.get("from", 0) / 1000.0, 3),
                      "end":   round(off.get("to", 0) / 1000.0, 3),
                      "text":  txt})
    return words


def duration(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(video)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out or 0)



def _norm(t):
    return re.sub(r"[^\w']", "", t).lower()


def align_script(words, script):
    """Replace recognised words with the true script, keeping Whisper's timing.

    When the audio came from TTS, the script is ground truth and the recogniser
    is only useful for *when* each word is spoken. Aligning the two sequences
    gives perfectly spelled captions with real timings, and removes any need for
    vocab priming or spelling fixes on that video.
    """
    import difflib

    script_words = [w for w in re.split(r"\s+", script.strip()) if w]
    if not script_words or not words:
        return words

    a = [_norm(w["text"]) for w in words]
    b = [_norm(w) for w in script_words]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)

    out = []

    def spread(items, t0, t1):
        """Lay words across a span, weighted by length so long words hold longer."""
        span = max(t1 - t0, 0.04 * len(items))
        weights = [max(len(_norm(x)), 1) for x in items]
        total = float(sum(weights))
        t = t0
        for w, wt in zip(items, weights):
            d = span * (wt / total)
            out.append({"start": round(t, 3), "end": round(t + d, 3), "text": w})
            t += d

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append({"start": words[i1 + k]["start"],
                            "end":   words[i1 + k]["end"],
                            "text":  script_words[j1 + k]})
        elif tag == "replace":
            spread(script_words[j1:j2], words[i1]["start"], words[i2 - 1]["end"])
        elif tag == "delete":
            continue                      # heard but not in the script -- drop it
        elif tag == "insert":
            prev_end = out[-1]["end"] if out else (words[i1]["start"] if i1 < len(words) else 0.0)
            nxt = words[i1]["start"] if i1 < len(words) else prev_end + 0.4 * (j2 - j1)
            if nxt - prev_end < 0.12 * (j2 - j1) and i1 < len(words):
                # no real gap: borrow the front of the next spoken word
                nxt = words[i1]["start"] + (words[i1]["end"] - words[i1]["start"]) * 0.5
            spread(script_words[j1:j2], prev_end, max(nxt, prev_end + 0.12 * (j2 - j1)))

    # opcodes already arrive in script order -- never re-sort by time, or a stray
    # match on a common word drags it out of the sentence it belongs to.
    for i in range(1, len(out)):
        if out[i]["start"] < out[i - 1]["start"]:
            out[i]["start"] = out[i - 1]["start"]
    for i in range(len(out) - 1):
        if out[i]["end"] > out[i + 1]["start"]:
            out[i]["end"] = out[i + 1]["start"]
        if out[i]["end"] <= out[i]["start"]:
            out[i]["end"] = out[i]["start"] + 0.06
    return [w for w in out if w["text"]]


def ass_color(hexrgb, alpha=0):
    h = hexrgb.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ts(t):
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def esc(t):
    return t.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _measure(font, size, text):
    """Width/height of a word, so the box behind it can be drawn to fit.

    libass renders through fontconfig+freetype; cairo's toy API uses the same
    stack, so the numbers line up closely enough that padding absorbs the rest.
    """
    try:
        import cairo
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
        cr = cairo.Context(surf)
        cr.select_font_face(font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(size)
        adv = cr.text_extents(text).x_advance
        ascent, descent = cr.font_extents()[0], cr.font_extents()[1]
        return adv, ascent + descent
    except Exception:
        # crude fallback: average advance of a bold sans is ~0.58em
        return len(text) * size * 0.58, size * 1.25


def _round_rect(w, h, r):
    """ASS drawing for a rounded rectangle with its top-left at the origin.

    libass positions a drawing under \\an5 by shifting it half its own width and
    height, as though the path grew right and down from the origin -- so the path
    must start at (0,0), not be pre-centred, or it lands a full box off-target.
    """
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    k = r * 0.55228            # circular-arc bezier constant
    L, R, T, B = 0.0, w, 0.0, h
    if r <= 0.5:
        return f"m {L:.0f} {T:.0f} l {R:.0f} {T:.0f} {R:.0f} {B:.0f} {L:.0f} {B:.0f}"
    return (
        f"m {L + r:.0f} {T:.0f} "
        f"l {R - r:.0f} {T:.0f} "
        f"b {R - r + k:.0f} {T:.0f} {R:.0f} {T + r - k:.0f} {R:.0f} {T + r:.0f} "
        f"l {R:.0f} {B - r:.0f} "
        f"b {R:.0f} {B - r + k:.0f} {R - r + k:.0f} {B:.0f} {R - r:.0f} {B:.0f} "
        f"l {L + r:.0f} {B:.0f} "
        f"b {L + r - k:.0f} {B:.0f} {L:.0f} {B - r + k:.0f} {L:.0f} {B - r:.0f} "
        f"l {L:.0f} {T + r:.0f} "
        f"b {L:.0f} {T + r - k:.0f} {L + r - k:.0f} {T:.0f} {L + r:.0f} {T:.0f}"
    )


def build_ass(words, W, H, cfg):
    fs      = cfg["fontsize"] or max(24, int(H * cfg["fontscale"]))
    bord    = max(2, int(fs * 0.10))
    padx    = max(4, int(fs * 0.24))
    pady    = max(3, int(fs * 0.14))
    marginv = cfg["marginv"] or int(H * cfg["marginscale"])
    font    = cfg["font"]

    textc   = ass_color(cfg.get("textcolor", "FFFFFF"))
    borderc = ass_color(cfg.get("bordercolor", "000000"))
    shadowc = ass_color("000000")
    box_rgb = cfg["boxcolor"].lstrip("#")
    box_a   = int(round((1.0 - cfg.get("boxopacity", 100) / 100.0) * 255))
    radius  = float(cfg.get("boxradius", 0))

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Box,{font},{fs},{textc},{textc},{shadowc},{shadowc},-1,0,0,0,100,100,0,0,1,0,0,5,20,20,20,1
Style: Text,{font},{fs},{textc},{textc},{borderc},{shadowc},-1,0,0,0,100,100,0,0,1,{bord},0,5,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    a, b = cfg["pop_from"], cfg["pop_over"]
    d1, d2 = cfg["pop_in_ms"], cfg["pop_settle_ms"]
    anim = (f"\\fscx{a}\\fscy{a}\\t(0,{d1},\\fscx{b}\\fscy{b})"
            f"\\t({d1},{d2},\\fscx100\\fscy100)")

    lines, n = [], len(words)
    for i, w in enumerate(words):
        txt = w["text"]
        if cfg["strip_punct"]:
            txt = re.sub(r"[,.]+$", "", txt)
        if cfg["upper"]:
            txt = txt.upper()
        if not txt:
            continue

        start, end = w["start"] + cfg["offset"], w["end"] + cfg["offset"]
        nxt = (words[i + 1]["start"] + cfg["offset"]) if i + 1 < n else None
        end += cfg["hold"]
        if end - start < cfg["min_dur"]:
            end = start + cfg["min_dur"]
        if nxt is not None:
            end = min(end, nxt)
            if end <= start:
                end = start + 0.08

        tw, th = _measure(font, fs, txt)
        bw, bh = tw + 2 * padx, th + 2 * pady
        cx = W / 2.0
        cy = H - marginv - bh / 2.0
        r_px = radius / 100.0 * (bh / 2.0)

        t = esc(txt)
        pos = f"\\an5\\pos({cx:.0f},{cy:.0f})"
        if box_a < 255:
            shape = _round_rect(bw, bh, r_px)
            lines.append(
                f"Dialogue: 0,{ts(start)},{ts(end)},Box,,0,0,0,,"
                f"{{{pos}\\1c&H{box_rgb[4:6]}{box_rgb[2:4]}{box_rgb[0:2]}&"
                f"\\1a&H{box_a:02X}&\\bord0\\shad0{anim}\\p1}}{shape}{{\\p0}}")
        lines.append(f"Dialogue: 1,{ts(start)},{ts(end)},Text,,0,0,0,,"
                     f"{{{pos}{anim}}}{t}")
    return head + "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("-o", "--out")
    p.add_argument("--model", default=str(MODEL_DIR / "ggml-base.bin"))
    p.add_argument("--whisper-bin", default=str(WHISPER_BIN), dest="whisper_bin")
    p.add_argument("--dtw", default="base", help="DTW preset matching the model; '' to disable")
    p.add_argument("--lang", default=None)
    p.add_argument("--script", default=None,
                   help="text file of exactly what is said; words come from it, "
                        "timing from the audio (ideal for TTS voice-overs)")
    p.add_argument("--carry", action="store_true",
                   help="repeat the vocab prompt in every window (stronger, riskier)")
    p.add_argument("--vocab", default=str(HERE / "vocab.txt"),
                   help="recurring terms, one per line; primes the recogniser")
    p.add_argument("--fixes", default=str(HERE / "fixes.txt"),
                   help="'wrong => right' lines applied after transcription")
    p.add_argument("--font", default="Ubuntu Sans")
    p.add_argument("--fontsize", type=int, default=0, help="absolute px; overrides --fontscale")
    p.add_argument("--fontscale", type=float, default=0.050,
                   help="text height as a fraction of frame height")
    p.add_argument("--marginscale", type=float, default=0.135,
                   help="distance off the bottom as a fraction of frame height")
    p.add_argument("--offset", type=float, default=0.10,
                   help="shift all captions later by N seconds to match the voice")
    p.add_argument("--marginv", type=int, default=0)
    p.add_argument("--boxcolor", default="FFD400", help="box fill, hex RGB")
    p.add_argument("--textcolor", default="FFFFFF", help="letter fill, hex RGB")
    p.add_argument("--bordercolor", default="000000", help="letter outline, hex RGB")
    p.add_argument("--boxradius", type=float, default=0,
                   help="corner rounding, 0-100%% of half the box height (100 = pill)")
    p.add_argument("--boxopacity", type=float, default=100,
                   help="box opacity, 0-100%% (0 hides the box entirely)")
    p.add_argument("--hold", type=float, default=0.35)
    p.add_argument("--min-dur", type=float, default=0.18, dest="min_dur")
    p.add_argument("--pop-from", type=int, default=55, dest="pop_from")
    p.add_argument("--pop-over", type=int, default=112, dest="pop_over")
    p.add_argument("--pop-in-ms", type=int, default=90, dest="pop_in_ms")
    p.add_argument("--pop-settle-ms", type=int, default=170, dest="pop_settle_ms")
    p.add_argument("--no-upper", action="store_false", dest="upper")
    p.add_argument("--keep-punct", action="store_false", dest="strip_punct")
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--preset", default="medium")
    p.add_argument("--reuse", action="store_true", help="reuse cached words.json (fast style iteration)")
    p.add_argument("--ass-only", action="store_true")
    args = p.parse_args()

    video = str(Path(args.video).resolve())
    work = Path(video).with_suffix("")
    wav, wjson, assf = f"{work}.16k.wav", f"{work}.words.json", f"{work}.ass"
    out = args.out or f"{work}.captioned.mp4"

    W, H = probe(video)
    print(f"[i] source {W}x{H}")

    if args.reuse and os.path.exists(wjson):
        words = json.load(open(wjson))
        print(f"[i] reusing {len(words)} cached words")
    else:
        print("[1/3] extracting audio")
        extract_audio(video, wav)
        print("[2/3] transcribing with whisper.cpp ...")
        prompt = load_vocab(args.vocab)
        if prompt:
            print(f"[i] priming with {len(prompt.split(','))} vocab terms")
        words = transcribe(wav, args.whisper_bin, args.model, args.lang, args.dtw, prompt, args.carry)
        fixes = load_fixes(args.fixes)
        if fixes and not args.script:
            words = apply_fixes(words, fixes)
            print(f"[i] applied {len(fixes)} spelling fixes")
        json.dump(words, open(wjson, "w"), indent=1)
        print(f"[i] {len(words)} words")

    # the script is ground truth whether the words were just recognised or reused
    if args.script and os.path.exists(args.script):
        words = align_script(words, open(args.script).read())
        print(f"[i] aligned to script: {len(words)} words, spelling from the script")

    open(assf, "w").write(build_ass(words, W, H, vars(args)))
    print(f"[i] wrote {assf}")
    if args.ass_only:
        return

    print("[3/3] burning captions ...")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-stats", "-i", video,
                    "-vf", f"ass={assf}",
                    "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", out], check=True)
    print(f"[done] {out}")


if __name__ == "__main__":
    main()
