"""Shortify — burn one-word-at-a-time captions onto a video.  See DESIGN.md."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango

import json, os, re, subprocess, sys, tempfile, threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import shortify as engine

from ribbon import Ribbon


def css():
    p = Gtk.CssProvider()
    p.load_from_path(str(HERE / "style.css"))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def tc(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def klass(w, *names):
    for n in names:
        w.get_style_context().add_class(n)
    return w


class WordRow(Gtk.ListBoxRow):
    def __init__(self, idx, word):
        super().__init__()
        klass(self, "sf-row")
        self.idx = idx
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.time = klass(Gtk.Label(label=tc(word["start"]), xalign=0), "sf-time")
        self.time.set_size_request(64, -1)
        self.label = klass(Gtk.Label(label=word["text"], xalign=0), "sf-word")
        self.dur = klass(Gtk.Label(label=f'{word["end"]-word["start"]:.2f}s', xalign=1),
                         "sf-time")
        box.pack_start(self.time, False, False, 0)
        box.pack_start(self.label, True, True, 0)
        box.pack_end(self.dur, False, False, 0)
        self.add(box)

    def refresh(self, word):
        self.label.set_text(word["text"])
        self.time.set_text(tc(word["start"]))
        ctx = self.label.get_style_context()
        if word.get("edited"):
            ctx.add_class("sf-word-edited")
        else:
            ctx.remove_class("sf-word-edited")


class Shortify(Gtk.Window):
    def __init__(self):
        super().__init__(title="Shortify")
        klass(self, "shortify")
        self.set_default_size(1060, 620)
        self.video = None
        self.words = []
        self.originals = {}
        self.duration = 0.0
        self.busy = False
        self.tmp = Path(tempfile.mkdtemp(prefix="shortify-"))

        root = klass(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "sf-root")
        self.add(root)
        root.pack_start(self._header(), False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.add_named(self._empty(), "empty")
        self.stack.add_named(self._work(), "work")
        root.pack_start(self.stack, True, True, 0)
        root.pack_start(self._bar(), False, False, 0)

        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._dropped)
        self.connect("destroy", Gtk.main_quit)
        self.stack.set_visible_child_name("empty")

    # ---------- shell ----------
    def _header(self):
        h = klass(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16), "sf-header")
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.pack_start(klass(Gtk.Label(label="SHORTIFY", xalign=0), "sf-title"), False, False, 0)
        left.pack_start(klass(Gtk.Label(label="SPOKEN WORD → BURNED CAPTION", xalign=0),
                              "sf-sub"), False, False, 0)
        h.pack_start(left, False, False, 0)
        self.filelbl = klass(Gtk.Label(label="no video loaded", xalign=1), "sf-file")
        self.filelbl.set_ellipsize(Pango.EllipsizeMode.START)
        h.pack_start(self.filelbl, True, True, 0)
        b = klass(Gtk.Button(label="Open video…"), "sf-btn")
        b.connect("clicked", lambda *_: self._choose())
        h.pack_end(b, False, False, 0)
        return h

    def _empty(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_border_width(40)
        d = klass(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), "sf-drop")
        d.set_valign(Gtk.Align.CENTER)
        d.set_halign(Gtk.Align.CENTER)
        d.set_size_request(520, 240)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_valign(Gtk.Align.CENTER)
        inner.pack_start(klass(Gtk.Label(label="Drop a video here"), "sf-drop-title"), False, False, 0)
        inner.pack_start(klass(Gtk.Label(label="or use Open video… — captions are "
                                               "transcribed on this machine, nothing uploads"),
                               "sf-drop-sub"), False, False, 0)
        d.pack_start(inner, True, True, 0)
        outer.pack_start(d, True, True, 0)
        return outer

    def _work(self):
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list = klass(Gtk.ListBox(), "sf-ledger")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-selected", self._row_selected)
        sw.add(self.list)
        psw = Gtk.ScrolledWindow()
        psw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        psw.set_overlay_scrolling(False)
        psw.add(self._panel())
        psw.set_size_request(360, -1)
        split.pack1(sw, True, False)
        split.pack2(psw, False, False)
        split.set_position(520)
        top.pack_start(split, True, True, 0)

        rsw = Gtk.ScrolledWindow()
        rsw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        klass(rsw, "sf-ribbon")
        self.ribbon = Ribbon()
        self.ribbon.connect("word-picked", lambda _r, i: self._pick(i))
        rsw.add(self.ribbon)
        self.rsw = rsw
        rsw.set_size_request(-1, 66)
        top.pack_start(rsw, False, False, 0)
        return top

    def _panel(self):
        p = klass(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), "sf-panel")
        p.set_size_request(340, -1)

        self.preview = klass(Gtk.Image(), "sf-preview")
        self.preview.set_size_request(132, 235)
        holder = Gtk.Box()
        holder.set_halign(Gtk.Align.CENTER)
        holder.pack_start(self.preview, False, False, 0)
        p.pack_start(holder, False, False, 0)

        p.pack_start(klass(Gtk.Label(label="SELECTED WORD", xalign=0), "sf-label"), False, False, 0)
        self.edit = klass(Gtk.Entry(), "sf-edit")
        self.edit.set_placeholder_text("pick a word in the ribbon")
        self.edit.connect("activate", lambda *_: self._commit())
        p.pack_start(self.edit, False, False, 0)
        self.editinfo = klass(Gtk.Label(label="", xalign=0), "sf-time")
        p.pack_start(self.editinfo, False, False, 0)

        p.pack_start(Gtk.Separator(), False, False, 4)
        self.sliders = {}
        for key, label, lo, hi, val, step in (
                ("fontscale",   "TEXT SIZE",     0.030, 0.090, 0.050, 0.002),
                ("marginscale", "HEIGHT",        0.060, 0.300, 0.135, 0.005),
                ("offset",      "TIMING NUDGE", -0.300, 0.600, 0.100, 0.010)):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lab = klass(Gtk.Label(label=label, xalign=0), "sf-label")
            lab.set_size_request(96, -1)
            val_l = klass(Gtk.Label(label=f"{val:.3f}", xalign=1), "sf-value")
            val_l.set_size_request(48, -1)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
            sc.set_value(val)
            sc.set_draw_value(False)
            sc.connect("value-changed", self._slider_moved, key, val_l)
            row.pack_start(lab, False, False, 0)
            row.pack_start(sc, True, True, 0)
            row.pack_start(val_l, False, False, 0)
            p.pack_start(row, False, False, 0)
            self.sliders[key] = sc

        crow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        crow.pack_start(klass(Gtk.Label(label="BOX COLOUR", xalign=0), "sf-label"), False, False, 0)
        self.color = Gtk.ColorButton()
        self.color.set_rgba(Gdk.RGBA(1.0, 0.831, 0.0, 1.0))
        self.color.connect("color-set", lambda *_: self._queue_preview())
        crow.pack_end(self.color, False, False, 0)
        p.pack_start(crow, False, False, 0)

        self.caps = Gtk.CheckButton(label="ALL CAPS")
        self.caps.set_active(True)
        self.caps.connect("toggled", lambda *_: self._queue_preview())
        p.pack_start(self.caps, False, False, 0)
        return p

    def _bar(self):
        b = klass(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12), "sf-bar")
        self.progress = Gtk.ProgressBar()
        self.progress.set_size_request(220, -1)
        self.progress.set_valign(Gtk.Align.CENTER)
        b.pack_start(self.progress, False, False, 0)
        self.status = klass(Gtk.Label(label="Drop a video to begin", xalign=0), "sf-status")
        b.pack_start(self.status, True, True, 0)

        self.btn_fix = klass(Gtk.Button(label="Save corrections"), "sf-btn")
        self.btn_fix.connect("clicked", lambda *_: self._save_fixes())
        self.btn_fix.set_sensitive(False)
        b.pack_end(self.btn_fix, False, False, 0)

        self.btn_burn = klass(Gtk.Button(label="Burn captions"), "sf-btn", "sf-primary")
        self.btn_burn.connect("clicked", lambda *_: self._burn())
        self.btn_burn.set_sensitive(False)
        b.pack_end(self.btn_burn, False, False, 0)

        self.btn_tr = klass(Gtk.Button(label="Transcribe"), "sf-btn")
        self.btn_tr.connect("clicked", lambda *_: self._transcribe())
        self.btn_tr.set_sensitive(False)
        b.pack_end(self.btn_tr, False, False, 0)
        return b

    # ---------- state ----------
    def _say(self, msg, kind=""):
        self.status.set_text(msg)
        ctx = self.status.get_style_context()
        for c in ("sf-status-work", "sf-status-fail"):
            ctx.remove_class(c)
        if kind:
            ctx.add_class(kind)

    def _lock(self, busy):
        self.busy = busy
        self.btn_tr.set_sensitive(not busy and self.video is not None)
        self.btn_burn.set_sensitive(not busy and bool(self.words))
        self.btn_fix.set_sensitive(not busy and bool(self.originals))

    def _choose(self):
        d = Gtk.FileChooserDialog(title="Choose a video", parent=self,
                                  action=Gtk.FileChooserAction.OPEN)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        f = Gtk.FileFilter()
        f.set_name("Video")
        for pat in ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.avi", "*.m4v"):
            f.add_pattern(pat)
        d.add_filter(f)
        if d.run() == Gtk.ResponseType.OK:
            self._load(d.get_filename())
        d.destroy()

    def _dropped(self, _w, _c, _x, _y, data, _i, _t):
        uris = data.get_uris()
        if uris:
            from urllib.parse import unquote, urlparse
            self._load(unquote(urlparse(uris[0]).path))

    def open_path(self, path):
        self._load(path)
        cache = Path(path).with_suffix("").as_posix() + ".words.json"
        if os.path.exists(cache):
            try:
                self._transcribed(json.load(open(cache)))
                self._say(f"{len(self.words)} words loaded from cached transcript")
            except Exception:
                pass
        return False

    def _load(self, path):
        if not path or not os.path.exists(path):
            return
        self.video = path
        self.words, self.originals = [], {}
        self.list.foreach(lambda c: self.list.remove(c))
        try:
            self.duration = engine.duration(path)
            self.W, self.H = engine.probe(path)
        except Exception as e:
            self._say(f"Cannot read that file: {e}", "sf-status-fail")
            return
        self.filelbl.set_text(os.path.basename(path))
        self.stack.set_visible_child_name("work")
        self.ribbon.load([], self.duration)
        self._lock(False)
        self._say(f"{self.W}×{self.H} · {self.duration:.0f}s — press Transcribe")
        self._queue_preview()

    # ---------- transcribe ----------
    def _transcribe(self):
        self._lock(True)
        self.progress.set_fraction(0)
        self._say("Transcribing on this machine…", "sf-status-work")

        def work():
            try:
                wav = str(self.tmp / "audio.wav")
                engine.extract_audio(self.video, wav)
                prompt = engine.load_vocab(str(ROOT / "vocab.txt"))
                words = engine.transcribe(
                    wav, engine.WHISPER_BIN, str(engine.MODEL_DIR / "ggml-base.bin"),
                    None, "base", prompt, False,
                    progress_cb=lambda p: GLib.idle_add(self.progress.set_fraction, p / 100))
                words = engine.apply_fixes(words, engine.load_fixes(str(ROOT / "fixes.txt")))
                GLib.idle_add(self._transcribed, words)
            except Exception as e:
                GLib.idle_add(self._failed, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _transcribed(self, words):
        self.words = words
        self.originals = {}
        self.list.foreach(lambda c: self.list.remove(c))
        for i, w in enumerate(words):
            self.list.add(WordRow(i, w))
        self.list.show_all()
        self.ribbon.load(words, self.duration, self.sliders["offset"].get_value())
        self.progress.set_fraction(1.0)
        self._lock(False)
        self._say(f"{len(words)} words — click the ribbon to check timing, fix any wrong word")
        if words:
            self._pick(0)

    def _failed(self, msg):
        self._lock(False)
        self.progress.set_fraction(0)
        self._say(msg, "sf-status-fail")

    # ---------- editing ----------
    def _row_selected(self, _lb, row):
        if row is not None and not getattr(self, "_syncing", False):
            self._pick(row.idx, from_list=True)

    def _pick(self, idx, from_list=False):
        if not (0 <= idx < len(self.words)):
            return
        self.cur = idx
        w = self.words[idx]
        self.edit.set_text(w["text"])
        self.editinfo.set_text(f'{tc(w["start"])} → {tc(w["end"])}   '
                               f'{w["end"]-w["start"]:.2f}s')
        self.ribbon.select(idx)
        self._scroll_ribbon(idx)
        if not from_list:
            self._syncing = True
            row = self.list.get_row_at_index(idx)
            if row:
                self.list.select_row(row)
            self._syncing = False
        self._queue_preview()

    def _scroll_ribbon(self, idx):
        adj = self.rsw.get_hadjustment()
        if adj is None:
            return
        x = self.ribbon.chip_x(idx)
        page = adj.get_page_size()
        if x < adj.get_value() + 40 or x > adj.get_value() + page - 80:
            adj.set_value(max(0, min(x - page / 2, adj.get_upper() - page)))

    def _commit(self):
        i = getattr(self, "cur", -1)
        if not (0 <= i < len(self.words)):
            return
        new = self.edit.get_text().strip()
        if not new or new == self.words[i]["text"]:
            return
        old = self.words[i]["text"]
        self.originals.setdefault(i, old)
        self.words[i]["text"] = new
        self.words[i]["edited"] = True
        row = self.list.get_row_at_index(i)
        if row:
            row.refresh(self.words[i])
        self.ribbon.queue_draw()
        self._lock(False)
        self._say(f'"{old}" → "{new}"')
        self._queue_preview()

    def _save_fixes(self):
        path = ROOT / "fixes.txt"
        pairs = [(self.originals[i], self.words[i]["text"])
                 for i in sorted(self.originals)
                 if self.originals[i].lower() != self.words[i]["text"].lower()]
        if not pairs:
            self._say("No corrections to save")
            return
        existing = path.read_text() if path.exists() else ""
        added = 0
        with open(path, "a") as f:
            for a, b in pairs:
                line = f"{a} => {b}"
                if line not in existing:
                    f.write(line + "\n")
                    added += 1
        self._say(f"Saved {added} correction(s) to fixes.txt — they apply to every future video")

    # ---------- preview ----------
    def _slider_moved(self, sc, key, label):
        label.set_text(f"{sc.get_value():.3f}")
        if key == "offset":
            self.ribbon.set_offset(sc.get_value())
        self._queue_preview()

    def _queue_preview(self):
        if getattr(self, "_pv_id", None):
            GLib.source_remove(self._pv_id)
        self._pv_id = GLib.timeout_add(280, self._do_preview)

    def _cfg(self):
        c = self.color.get_rgba()
        hexcol = "%02X%02X%02X" % (int(c.red * 255), int(c.green * 255), int(c.blue * 255))
        return dict(fontsize=0, fontscale=self.sliders["fontscale"].get_value(),
                    marginv=0, marginscale=self.sliders["marginscale"].get_value(),
                    offset=self.sliders["offset"].get_value(),
                    boxcolor=hexcol, font="Ubuntu Sans",
                    upper=self.caps.get_active(), strip_punct=True,
                    hold=0.35, min_dur=0.18,
                    pop_from=55, pop_over=112, pop_in_ms=90, pop_settle_ms=170)

    def _do_preview(self):
        self._pv_id = None
        if not self.video or self.busy:
            return False
        idx = getattr(self, "cur", 0)
        if self.words and 0 <= idx < len(self.words):
            w = self.words[idx]
            t = (w["start"] + w["end"]) / 2 + self.sliders["offset"].get_value()
        else:
            t = min(1.0, self.duration / 2)

        def work():
            try:
                png = str(self.tmp / "pv.png")
                vf = "scale=132:-1"
                if self.words:
                    assf = str(self.tmp / "pv.ass")
                    open(assf, "w").write(engine.build_ass(self.words, self.W, self.H, self._cfg()))
                    vf = f"ass={assf},scale=132:-1"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-copyts", "-ss", f"{t:.2f}",
                                "-i", self.video, "-frames:v", "1", "-vf", vf, png],
                               check=True, timeout=60)
                GLib.idle_add(self._show_preview, png)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()
        return False

    def _show_preview(self, png):
        try:
            self.preview.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file(png))
        except Exception:
            pass

    # ---------- burn ----------
    def _burn(self):
        d = Gtk.FileChooserDialog(title="Save captioned video", parent=self,
                                  action=Gtk.FileChooserAction.SAVE)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        d.set_current_name(Path(self.video).stem + "-captioned.mp4")
        d.set_do_overwrite_confirmation(True)
        out = d.get_filename() if d.run() == Gtk.ResponseType.OK else None
        d.destroy()
        if not out:
            return

        self._lock(True)
        self.progress.set_fraction(0)
        self._say("Burning captions…", "sf-status-work")
        assf = str(self.tmp / "burn.ass")
        open(assf, "w").write(engine.build_ass(self.words, self.W, self.H, self._cfg()))

        def work():
            try:
                proc = subprocess.Popen(
                    ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
                     "-i", self.video, "-vf", f"ass={assf}",
                     "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                     "-movflags", "+faststart", out],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                for line in proc.stdout:
                    if line.startswith("out_time_ms="):
                        try:
                            done = int(line.split("=")[1]) / 1_000_000.0
                            GLib.idle_add(self.progress.set_fraction,
                                          min(1.0, done / max(self.duration, 0.1)))
                        except ValueError:
                            pass
                proc.wait()
                if proc.returncode != 0:
                    raise RuntimeError(proc.stderr.read().strip()[:200] or "ffmpeg failed")
                GLib.idle_add(self._burned, out)
            except Exception as e:
                GLib.idle_add(self._failed, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _burned(self, out):
        self.progress.set_fraction(1.0)
        self._lock(False)
        self._say(f"Done — {out}")


def main():
    css()
    win = Shortify()
    win.show_all()
    if len(sys.argv) > 1:
        GLib.idle_add(win.open_path, sys.argv[1])
    Gtk.main()


if __name__ == "__main__":
    main()
