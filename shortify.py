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


def build_ass(words, W, H, cfg):
    fs      = cfg["fontsize"] or max(24, int(H * cfg["fontscale"]))
    bord    = max(2, int(fs * 0.10))     # black outline around the letters
    pad     = max(4, int(fs * 0.16))     # yellow box padding
    marginv = cfg["marginv"] or int(H * cfg["marginscale"])
    white   = ass_color("FFFFFF")
    black   = ass_color("000000")
    yellow  = ass_color(cfg["boxcolor"])
    font    = cfg["font"]

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Box,{font},{fs},{white},{white},{yellow},{black},-1,0,0,0,100,100,0,0,3,{pad},0,2,40,40,{marginv},1
Style: Text,{font},{fs},{white},{white},{black},{black},-1,0,0,0,100,100,0,0,1,{bord},0,2,40,40,{marginv},1

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
        t = esc(txt)
        lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Box,,0,0,0,,{{\\1a&HFF&{anim}}}{t}")
        lines.append(f"Dialogue: 1,{ts(start)},{ts(end)},Text,,0,0,0,,{{{anim}}}{t}")
    return head + "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("-o", "--out")
    p.add_argument("--model", default=str(MODEL_DIR / "ggml-base.bin"))
    p.add_argument("--whisper-bin", default=str(WHISPER_BIN), dest="whisper_bin")
    p.add_argument("--dtw", default="base", help="DTW preset matching the model; '' to disable")
    p.add_argument("--lang", default=None)
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
    p.add_argument("--boxcolor", default="FFD400")
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
        if fixes:
            words = apply_fixes(words, fixes)
            print(f"[i] applied {len(fixes)} spelling fixes")
        json.dump(words, open(wjson, "w"), indent=1)
        print(f"[i] {len(words)} words")

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
