"""Shortify — burn one-word-at-a-time captions onto a video.  See DESIGN.md."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango

import json, os, signal, subprocess, sys, tempfile, threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import shortify as engine
from ribbon import Ribbon

CONFIG = Path(os.path.expanduser("~/.config/shortify/settings.json"))
THEMES = ROOT / "themes.json"
DEFAULTS = dict(fontscale=0.050, marginscale=0.135, offset=0.100,
                boxcolor="FFD400", textcolor="FFFFFF", bordercolor="000000",
                font="Ubuntu Sans", upper=True, sample="Sri Lanka",
                boxradius=0, boxopacity=100)

SHORT_WORD = 0.10      # below this, timing is probably a glitch
LONG_WORD  = 1.60      # above this, the caption hangs


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


def hex_of(rgba):
    return "%02X%02X%02X" % (int(rgba.red * 255 + 0.5),
                             int(rgba.green * 255 + 0.5),
                             int(rgba.blue * 255 + 0.5))


def rgba_of(hexs):
    r = Gdk.RGBA()
    r.parse("#" + hexs)
    return r


def mark_drift(words):
    for w in words:
        d = w["end"] - w["start"]
        w["drift"] = d < SHORT_WORD or d > LONG_WORD
    return words


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
        self.refresh(word)

    def refresh(self, word):
        self.label.set_text(word["text"])
        self.time.set_text(tc(word["start"]))
        for w, cls in ((self.label, "sf-word-edited"), (self.dur, "sf-time-drift")):
            ctx = w.get_style_context()
            hit = word.get("edited") if cls.endswith("edited") else word.get("drift")
            ctx.add_class(cls) if hit else ctx.remove_class(cls)


class Shortify(Gtk.Window):
    def __init__(self):
        super().__init__(title="Shortify")
        klass(self, "shortify")
        self.set_default_size(1090, 648)
        self.video = None
        self.words = []
        self.originals = {}
        self.duration = 0.0
        self.busy = False
        self.proc = None
        self.cur = -1
        self._ready = False
        self.tmp = Path(tempfile.mkdtemp(prefix="shortify-"))
        self.cfgv = self._load_settings()

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
        self.connect("destroy", self._quit)
        self.connect("key-press-event", self._keys)
        self.stack.set_visible_child_name("empty")
        self._ready = True          # widgets all exist; signals may now persist state

    # ---------- settings ----------
    def _load_settings(self):
        v = dict(DEFAULTS)
        try:
            v.update(json.load(open(CONFIG)))
        except Exception:
            pass
        return v

    def _load_themes(self):
        try:
            return json.load(open(THEMES))
        except Exception:
            return {}

    def _theme_picked(self, cb):
        name = cb.get_active_text()
        theme = self.themes.get(name)
        if not theme:
            return
        self._applying = True          # one preview at the end, not one per widget
        try:
            for k in ("fontscale", "marginscale", "boxradius", "boxopacity"):
                if k in theme:
                    self.sliders[k].set_value(theme[k])
                    vl, fmt = self.slabels[k]
                    vl.set_text(fmt.format(theme[k]))
            for k in ("textcolor", "bordercolor", "boxcolor"):
                if k in theme:
                    self.colors[k].set_rgba(rgba_of(theme[k]))
            if "font" in theme:
                self.fontbtn.set_font(theme["font"] + " 12")
            if "upper" in theme:
                self.caps.set_active(bool(theme["upper"]))
        finally:
            self._applying = False
        self._changed()
        self._say(f"Theme: {name} — tweak any control to make it your own")

    def _save_settings(self):
        if not self._ready:
            return
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            json.dump(self._style(), open(CONFIG, "w"), indent=1)
        except Exception:
            pass

    def _quit(self, *_):
        self._save_settings()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        Gtk.main_quit()

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
        d.set_valign(Gtk.Align.CENTER); d.set_halign(Gtk.Align.CENTER)
        d.set_size_request(520, 220)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_valign(Gtk.Align.CENTER)
        inner.pack_start(klass(Gtk.Label(label="Drop a video here"), "sf-drop-title"), False, False, 0)
        inner.pack_start(klass(Gtk.Label(label="or use Open video… — transcribed on this "
                                               "machine, nothing uploads"), "sf-drop-sub"),
                         False, False, 0)
        d.pack_start(inner, True, True, 0)
        outer.pack_start(d, True, True, 0)
        return outer

    def _work(self):
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_size_request(250, -1)
        self.list = klass(Gtk.ListBox(), "sf-ledger")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-selected", self._row_selected)
        sw.add(self.list)

        psw = Gtk.ScrolledWindow()
        psw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        psw.set_overlay_scrolling(False)
        psw.add(self._panel())
        psw.set_size_request(660, -1)

        split.pack1(sw, True, False)
        split.pack2(psw, False, False)
        split.set_position(310)
        top.pack_start(split, True, True, 0)

        rsw = Gtk.ScrolledWindow()
        rsw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        klass(rsw, "sf-ribbon")
        self.ribbon = Ribbon()
        self.ribbon.connect("word-picked", lambda _r, i: self._pick(i))
        rsw.add(self.ribbon)
        rsw.set_size_request(-1, 66)
        self.rsw = rsw
        top.pack_start(rsw, False, False, 0)
        top.pack_start(self._savebar(), False, False, 0)
        return top

    def _savebar(self):
        b = klass(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8), "sf-save")
        b.pack_start(klass(Gtk.Label(label="SAVE TO", xalign=0), "sf-label"), False, False, 0)
        self.outpath = klass(Gtk.Entry(), "sf-path")
        self.outpath.set_placeholder_text("output file")
        b.pack_start(self.outpath, True, True, 0)
        br = klass(Gtk.Button(label="Browse…"), "sf-btn")
        br.connect("clicked", lambda *_: self._browse_out())
        b.pack_start(br, False, False, 0)
        self.btn_play = klass(Gtk.Button(label="▶  Play source"), "sf-btn")
        self.btn_play.connect("clicked", lambda *_: self._play())
        b.pack_start(self.btn_play, False, False, 0)
        self.outpath.connect("changed", lambda *_: self._refresh_play())
        return b

    def _refresh_play(self):
        """Say which file Play will open, so a missing render is obvious."""
        out = self.outpath.get_text().strip()
        done = bool(out) and os.path.exists(out)
        self.btn_play.set_label("▶  Play result" if done else "▶  Play source")
        self.btn_play.set_tooltip_text(
            out if done else "Not rendered yet — this opens the original. "
                             "Press Burn & Save to create the captioned file.")
        ctx = self.btn_play.get_style_context()
        ctx.add_class("sf-primary") if done else ctx.remove_class("sf-primary")

    def _panel(self):
        """Preview sits beside the controls — the laptop panel is only 768px tall."""
        p = klass(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6), "sf-panel")
        p.set_size_request(640, -1)

        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.editlabel = klass(Gtk.Label(label="SAMPLE WORD", xalign=0), "sf-label")
        r.pack_start(self.editlabel, False, False, 0)
        self.edit = klass(Gtk.Entry(), "sf-edit")
        self.edit.set_placeholder_text("type a word to preview the style")
        self.edit.set_text(self.cfgv.get("sample", "Sri Lanka"))
        self.edit.connect("activate", lambda *_: self._commit(advance=True))
        self.edit.connect("changed", self._edit_changed)
        r.pack_start(self.edit, False, False, 0)
        self.editinfo = klass(Gtk.Label(label="a stand-in so you can set the style "
                                              "before transcribing", xalign=0), "sf-time")
        r.pack_start(self.editinfo, False, False, 0)
        r.pack_start(Gtk.Separator(), False, False, 2)

        trow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tl = klass(Gtk.Label(label="THEME", xalign=0), "sf-label")
        tl.set_size_request(54, -1)
        self.themes = self._load_themes()
        self.themebox = Gtk.ComboBoxText()
        self.themebox.append_text("— custom —")
        for name in self.themes:
            self.themebox.append_text(name)
        self.themebox.set_active(0)
        self.themebox.connect("changed", self._theme_picked)
        trow.pack_start(tl, False, False, 0)
        trow.pack_start(self.themebox, True, True, 0)
        r.pack_start(trow, False, False, 0)

        self.sliders, self.slabels = {}, {}
        for key, label, lo, hi, step, fmt in (
                ("fontscale",   "SIZE",    0.030, 0.090, 0.002, "{:.3f}"),
                ("marginscale", "HEIGHT",  0.060, 0.300, 0.005, "{:.3f}"),
                ("offset",      "NUDGE",  -0.300, 0.600, 0.010, "{:+.2f}s"),
                ("boxradius",   "RADIUS",  0,     100,   1,     "{:.0f}%"),
                ("boxopacity",  "OPACITY", 0,     100,   1,     "{:.0f}%")):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lab = klass(Gtk.Label(label=label, xalign=0), "sf-label")
            lab.set_size_request(54, -1)
            vl = klass(Gtk.Label(label=fmt.format(self.cfgv[key]), xalign=1), "sf-value")
            vl.set_size_request(52, -1)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
            sc.set_value(self.cfgv[key]); sc.set_draw_value(False)
            sc.connect("value-changed", self._slider_moved, key, vl, fmt)
            self.slabels[key] = (vl, fmt)
            row.pack_start(lab, False, False, 0)
            row.pack_start(sc, True, True, 0)
            row.pack_start(vl, False, False, 0)
            r.pack_start(row, False, False, 0)
            self.sliders[key] = sc

        frow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fl = klass(Gtk.Label(label="FONT", xalign=0), "sf-label")
        fl.set_size_request(54, -1)
        self.fontbtn = Gtk.FontButton()
        self.fontbtn.set_show_size(False)
        self.fontbtn.set_show_style(False)
        self.fontbtn.set_font(self.cfgv["font"] + " 12")
        self.fontbtn.connect("font-set", lambda *_: self._changed())
        frow.pack_start(fl, False, False, 0)
        frow.pack_start(self.fontbtn, True, True, 0)
        r.pack_start(frow, False, False, 0)

        crow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.colors = {}
        for key, label in (("textcolor", "TEXT"), ("bordercolor", "BORDER"),
                           ("boxcolor", "BOX")):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            cell.pack_start(klass(Gtk.Label(label=label, xalign=0.5), "sf-label"),
                            False, False, 0)
            cb = Gtk.ColorButton()
            cb.set_rgba(rgba_of(self.cfgv[key]))
            cb.connect("color-set", lambda *_: self._changed())
            cell.pack_start(cb, False, False, 0)
            crow.pack_start(cell, True, True, 0)
            self.colors[key] = cb
        r.pack_start(crow, False, False, 0)

        last = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.caps = Gtk.CheckButton(label="ALL CAPS")
        self.caps.set_active(self.cfgv["upper"])
        self.caps.connect("toggled", lambda *_: self._changed())
        last.pack_start(self.caps, False, False, 0)
        rb = klass(Gtk.Button(label="Reset style"), "sf-btn")
        rb.connect("clicked", lambda *_: self._reset_style())
        last.pack_end(rb, False, False, 0)
        r.pack_start(last, False, False, 0)

        cols.pack_start(r, True, True, 0)

        # the preview sits on the right, centred against the controls
        self.preview = klass(Gtk.Image(), "sf-preview")
        self.preview.set_size_request(200, 356)
        pv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pv.set_valign(Gtk.Align.CENTER)
        pv.set_halign(Gtk.Align.CENTER)
        pv.pack_start(self.preview, False, False, 0)
        cols.pack_end(pv, False, False, 0)

        p.pack_start(cols, True, True, 0)
        return p

    def _bar(self):
        b = klass(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12), "sf-bar")
        self.progress = Gtk.ProgressBar()
        self.progress.set_size_request(200, -1)
        self.progress.set_valign(Gtk.Align.CENTER)
        b.pack_start(self.progress, False, False, 0)
        self.status = klass(Gtk.Label(label="Drop a video to begin", xalign=0), "sf-status")
        self.status.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        b.pack_start(self.status, True, True, 0)

        self.btn_fix = klass(Gtk.Button(label="Save corrections"), "sf-btn")
        self.btn_fix.set_tooltip_text("Append your edits to fixes.txt so they apply "
                                      "to every future video")
        self.btn_fix.connect("clicked", lambda *_: self._save_fixes())
        self.btn_fix.set_sensitive(False)
        b.pack_end(self.btn_fix, False, False, 0)

        self.btn_burn = klass(Gtk.Button(label="Burn & Save"), "sf-btn", "sf-primary")
        self.btn_burn.set_tooltip_text("Render the captions into a new video file "
                                       "at the SAVE TO location")
        self.btn_burn.connect("clicked", lambda *_: self._burn_or_cancel())
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
        self.btn_burn.set_sensitive(bool(self.words) or busy)
        self.btn_fix.set_sensitive(not busy and bool(self.originals))
        self.btn_burn.set_label("Cancel" if busy and self.proc else "Burn & Save")

    def _keys(self, _w, ev):
        # arrows belong to whatever control has focus -- a combo, slider or entry
        # must not have its keys stolen to walk the word list
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Entry, Gtk.ComboBox, Gtk.Scale, Gtk.Button,
                              Gtk.SpinButton, Gtk.ColorButton, Gtk.FontButton)):
            return False
        name = Gdk.keyval_name(ev.keyval)
        if name in ("Down", "Right") and self.words:
            self._pick(min(self.cur + 1, len(self.words) - 1)); return True
        if name in ("Up", "Left") and self.words:
            self._pick(max(self.cur - 1, 0)); return True
        if name == "space" and self.video:
            self._play(); return True
        return False

    def _choose(self):
        d = Gtk.FileChooserDialog(title="Choose a video", parent=self,
                                  action=Gtk.FileChooserAction.OPEN)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        f = Gtk.FileFilter(); f.set_name("Video")
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
        self.words, self.originals, self.cur = [], {}, -1
        self.list.foreach(lambda c: self.list.remove(c))
        try:
            self.duration = engine.duration(path)
            self.W, self.H = engine.probe(path)
        except Exception as e:
            self._say(f"Cannot read that file: {e}", "sf-status-fail")
            return
        self.filelbl.set_text(os.path.basename(path))
        self.editlabel.set_text("SAMPLE WORD")
        self.editinfo.set_text("a stand-in so you can set the style before transcribing")
        self.edit.set_text(self.cfgv.get("sample", "Sri Lanka"))
        self.outpath.set_text(str(Path(path).with_suffix("")) + "-captioned.mp4")
        self.stack.set_visible_child_name("work")
        self.ribbon.load([], self.duration)
        self._lock(False)
        self.btn_burn.set_sensitive(False)
        self._refresh_play()
        self._say(f"{self.W}×{self.H} · {self.duration:.0f}s — press Transcribe")
        self._queue_preview()

    # ---------- output ----------
    def _browse_out(self):
        d = Gtk.FileChooserDialog(title="Where should the captioned video go?",
                                  parent=self, action=Gtk.FileChooserAction.SAVE)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                      "Set location", Gtk.ResponseType.OK)
        cur = self.outpath.get_text().strip()
        if cur:
            d.set_current_folder(str(Path(cur).parent))
            d.set_current_name(Path(cur).name)
        d.set_do_overwrite_confirmation(True)
        if d.run() == Gtk.ResponseType.OK:
            self.outpath.set_text(d.get_filename())
            self._say("Location set — now press Burn & Save to render the video")
        d.destroy()

    def _play(self):
        out = self.outpath.get_text().strip()
        target = out if out and os.path.exists(out) else self.video
        if not target or not os.path.exists(target):
            self._say("Nothing to play yet", "sf-status-fail")
            return
        which = "captioned" if target == out else "ORIGINAL (not rendered yet)"
        try:
            subprocess.Popen(["xdg-open", target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._say(f"Opening the {which}: {os.path.basename(target)}")
        except Exception as e:
            self._say(f"Could not open player: {e}", "sf-status-fail")

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
        self.words = mark_drift(words)
        self.originals = {}
        self.list.foreach(lambda c: self.list.remove(c))
        for i, w in enumerate(self.words):
            self.list.add(WordRow(i, w))
        self.list.show_all()
        self.ribbon.load(self.words, self.duration, self.sliders["offset"].get_value())
        self.progress.set_fraction(1.0)
        self.editlabel.set_text("SELECTED WORD")
        self._lock(False)
        odd = sum(1 for w in self.words if w.get("drift"))
        note = f" · {odd} with odd timing (amber)" if odd else ""
        self._say(f"{len(self.words)} words{note} — fix any wrong word, "
                  f"then press Burn & Save")
        if self.words:
            self._pick(0)

    def _failed(self, msg):
        self.proc = None
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
                               f'{w["end"]-w["start"]:.2f}s'
                               + ("   odd timing" if w.get("drift") else ""))
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
            adj.set_value(max(0, min(x - page / 2, max(0, adj.get_upper() - page))))

    def _commit(self, advance=False):
        i = self.cur
        if not (0 <= i < len(self.words)):
            return
        new = self.edit.get_text().strip()
        if new and new != self.words[i]["text"]:
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
        if advance and i + 1 < len(self.words):
            self._pick(i + 1)
            self.edit.grab_focus()
            self.edit.select_region(0, -1)

    def _edit_changed(self, _e):
        """Before a transcript exists the box drives the sample word, not a correction."""
        if not self.words:
            self.cfgv["sample"] = self.edit.get_text()
            self._save_settings()
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
        self._say(f"Saved {added} correction(s) to fixes.txt — applied to every future video")

    # ---------- style ----------
    def _style(self):
        fam = Pango.FontDescription(self.fontbtn.get_font()).get_family() or "Ubuntu Sans"
        return dict(fontscale=self.sliders["fontscale"].get_value(),
                    marginscale=self.sliders["marginscale"].get_value(),
                    offset=self.sliders["offset"].get_value(),
                    textcolor=hex_of(self.colors["textcolor"].get_rgba()),
                    bordercolor=hex_of(self.colors["bordercolor"].get_rgba()),
                    boxcolor=hex_of(self.colors["boxcolor"].get_rgba()),
                    boxradius=self.sliders["boxradius"].get_value(),
                    boxopacity=self.sliders["boxopacity"].get_value(),
                    font=fam, upper=self.caps.get_active(),
                    sample=self.cfgv.get("sample", "Sri Lanka"))

    def _cfg(self):
        s = self._style()
        s.update(fontsize=0, marginv=0, strip_punct=True, hold=0.35, min_dur=0.18,
                 pop_from=55, pop_over=112, pop_in_ms=90, pop_settle_ms=170)
        return s

    def _reset_style(self):
        for k in ("fontscale", "marginscale", "offset", "boxradius", "boxopacity"):
            self.sliders[k].set_value(DEFAULTS[k])
            vl, fmt = self.slabels[k]
            vl.set_text(fmt.format(DEFAULTS[k]))
        for k in ("textcolor", "bordercolor", "boxcolor"):
            self.colors[k].set_rgba(rgba_of(DEFAULTS[k]))
        self.fontbtn.set_font(DEFAULTS["font"] + " 12")
        self.caps.set_active(DEFAULTS["upper"])
        self._changed()
        self._say("Style reset to defaults")

    def _changed(self):
        if getattr(self, "_applying", False):
            return
        self._save_settings()
        self._queue_preview()

    def _slider_moved(self, sc, key, label, fmt="{:.3f}"):
        label.set_text(fmt.format(sc.get_value()))
        if key == "offset":
            self.ribbon.set_offset(sc.get_value())
        self._changed()

    # ---------- preview ----------
    def _queue_preview(self):
        if getattr(self, "_pv_id", None):
            GLib.source_remove(self._pv_id)
        self._pv_id = GLib.timeout_add(300, self._do_preview)

    def _do_preview(self):
        self._pv_id = None
        if not self.video or self.busy:
            return False
        off = self.sliders["offset"].get_value()
        if self.words and 0 <= self.cur < len(self.words):
            w = self.words[self.cur]
            t = (w["start"] + w["end"]) / 2 + off
            words = self.words
        else:
            t = min(1.0, self.duration / 2)
            txt = self.edit.get_text().strip() or "Sri Lanka"
            # placed so it straddles t *after* build_ass applies the offset
            words = [{"start": t - 0.4 - off, "end": t + 0.4 - off, "text": txt}]

        def work():
            try:
                png = str(self.tmp / "pv.png")
                assf = str(self.tmp / "pv.ass")
                open(assf, "w").write(engine.build_ass(words, self.W, self.H, self._cfg()))
                vf = f"ass={assf},scale=200:-1"
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
    def _burn_or_cancel(self):
        if self.busy and self.proc and self.proc.poll() is None:
            self.proc.kill()
            self._say("Cancelled", "sf-status-fail")
            return
        self._burn()

    def _burn(self):
        out = self.outpath.get_text().strip()
        if not out:
            self._say("Set a save location first", "sf-status-fail")
            return
        if os.path.abspath(out) == os.path.abspath(self.video):
            self._say("Save location must differ from the source video", "sf-status-fail")
            return
        Path(out).parent.mkdir(parents=True, exist_ok=True)

        self._lock(True)
        self.progress.set_fraction(0)
        self._say("Burning captions…", "sf-status-work")
        assf = str(self.tmp / "burn.ass")
        open(assf, "w").write(engine.build_ass(self.words, self.W, self.H, self._cfg()))

        def work():
            try:
                self.proc = subprocess.Popen(
                    ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
                     "-i", self.video, "-vf", f"ass={assf}",
                     "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                     "-movflags", "+faststart", out],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                GLib.idle_add(self._lock, True)
                for line in self.proc.stdout:
                    if line.startswith("out_time_ms="):
                        try:
                            done = int(line.split("=")[1]) / 1_000_000.0
                            GLib.idle_add(self.progress.set_fraction,
                                          min(1.0, done / max(self.duration, 0.1)))
                        except ValueError:
                            pass
                self.proc.wait()
                rc = self.proc.returncode
                err = self.proc.stderr.read().strip()[:200]
                self.proc = None
                if rc == 0:
                    GLib.idle_add(self._burned, out)
                elif rc in (-9, 137):
                    GLib.idle_add(self._failed, "Cancelled")
                else:
                    GLib.idle_add(self._failed, err or f"ffmpeg failed ({rc})")
            except Exception as e:
                self.proc = None
                GLib.idle_add(self._failed, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _burned(self, out):
        self.progress.set_fraction(1.0)
        self.proc = None
        self._lock(False)
        mb = os.path.getsize(out) / 1e6
        self._refresh_play()
        self._say(f"Saved to {out}  ({mb:.0f} MB) — press Play result to watch")


def main():
    css()
    win = Shortify()
    win.show_all()
    if len(sys.argv) > 1:
        GLib.idle_add(win.open_path, sys.argv[1])
    Gtk.main()


if __name__ == "__main__":
    main()
