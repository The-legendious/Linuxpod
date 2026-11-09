"""
apple_style.py - the Apple-style look: rounded, antialiased ttk
controls (drawn with Pillow and installed as ttk image elements, since
no stock ttk theme draws rounded corners), a cleaner system font, and
the small bits of motion that make the app feel smooth - eased
scrolling for the artwork grids, an eased progress bar, cover art that
fades in instead of popping, and a fade-in when the window opens.

Everything here degrades quietly: without Pillow the controls fall
back to flat clam colors, and on a desktop without a compositor the
window fade is simply skipped by X.
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


# Font family every explicit font=(...) tuple in the app uses - set by
# init_fonts() once a Tk root exists. "TkDefaultFont" as a family name
# isn't resolved to the named font by Tk; it just falls through to
# fontconfig's default, so leaving it would ignore the choice below.
FAMILY = "TkDefaultFont"

# Clean, regular-weight sans-serifs, roughly in order of preference.
# Inter / SF Pro are deliberately not here: on many distros they ship
# as variable fonts, which Tk renders at the font's default instance -
# often Thin - so text came out as barely readable hairlines.
_FONT_CANDIDATES = (
    "Ubuntu", "Noto Sans", "Open Sans", "Roboto", "Cantarell",
    "Liberation Sans", "DejaVu Sans",
)


def _usable_font(name):
    """True if fontconfig has a static (non-variable) Regular face for
    this family and no variable face Tk might pick instead."""
    import subprocess
    try:
        out = subprocess.run(
            ["fc-list", "--format", "%{style}|%{variable}\n", f":family={name}"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None  # can't tell - caller decides
    has_regular = False
    for line in out.splitlines():
        style, _, variable = line.rpartition("|")
        if variable.strip().lower() == "true":
            return False
        styles = [x.strip().lower() for x in style.split(",")]
        if any(x in ("regular", "book", "normal", "roman") for x in styles) and not any(
            w in style.lower() for w in ("thin", "light", "hairline", "italic", "oblique")
        ):
            has_regular = True
    return has_regular

# macOS light-appearance palette.
CHROME = "#f2f2f5"        # sidebar / toolbar / status bar
CONTENT = "#ffffff"       # content panes
TEXT = "#1d1d1f"
SECONDARY = "#6e6e73"
TERTIARY = "#8e8e93"
SEPARATOR = "#e5e5ea"
ACCENT = "#0a6cf0"
ACCENT_PRESSED = "#0758c7"
ACCENT_SOFT = "#d6e6fd"
STRIPE = "#f6f6f8"
SIDEBAR_SELECTED = "#dcdce1"

SEG_TRACK = "#e3e3e8"

# Device card / capacity bar.
DARK_CARD_TOP = "#1f3b63"
DARK_CARD_BOTTOM = "#1f3b63"
DARK_CARD_MID = "#1f3b63"
CAPACITY_KINDS = (("audio", "Audio"), ("video", "Video"), ("other", "Other"), ("free", "Free"))
CAPACITY_COLORS = {"audio": "#64d2ff", "video": "#bf5af2", "other": "#ffd60a", "free": "#4d6890"}

_images = {}          # name -> PhotoImage (ttk elements need live references)
_elements_made = False


def init_fonts(root):
    """Pick the nicest installed sans-serif and point Tk's named fonts
    at it, so every ttk widget and every explicit font tuple picks it up."""
    global FAMILY
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        available = set()
    for name in _FONT_CANDIDATES:
        if name in available and _usable_font(name) is not False:
            FAMILY = name
            break
    else:
        try:
            FAMILY = tkfont.nametofont("TkDefaultFont", root=root).actual("family")
        except tk.TclError:
            return
    for named, size, weight in (
        ("TkDefaultFont", 10, "normal"), ("TkTextFont", 10, "normal"),
        ("TkMenuFont", 10, "normal"), ("TkHeadingFont", 9, "bold"),
        ("TkCaptionFont", 10, "bold"), ("TkSmallCaptionFont", 9, "normal"),
        ("TkTooltipFont", 9, "normal"), ("TkIconFont", 10, "normal"),
    ):
        try:
            tkfont.nametofont(named, root=root).configure(family=FAMILY, size=size, weight=weight)
        except tk.TclError:
            pass
    # Classic Tk menus (menu bar, right-click menus) - created after
    # this runs, so the option database is enough.
    root.option_add("*Menu.font", "TkMenuFont")
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Menu.borderWidth", 1)
    root.option_add("*Menu.activeBorderWidth", 0)
    root.option_add("*Menu.activeBackground", ACCENT)
    root.option_add("*Menu.activeForeground", "#ffffff")


def set_window_options(root, enabled):
    """Default background for dialogs/tiles created from now on."""
    root.option_add("*Toplevel.background", CONTENT if enabled else "", "interactive")
    root.option_add("*Menu.background", CONTENT if enabled else "", "interactive")


# --------------------------------------------------------------------------
# Drawing helpers (Pillow, supersampled for smooth edges)

_SS = 4  # supersampling factor


def _rgba(color, alpha=255):
    color = color.lstrip("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha)


def _rounded(w, h, radius, fill, outline=None, outline_width=1, shadow=False,
             inset=(0, 0, 0, 0)):
    """An antialiased rounded rectangle on a transparent w x h canvas.
    inset = transparent margin (left, top, right, bottom) around it."""
    big = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    l, t, r, b = (v * _SS for v in inset)
    box = [l, t, w * _SS - r - 1, h * _SS - b - 1]
    if shadow:
        # A 1px soft line under the control - the way macOS buttons sit
        # on the window rather than floating.
        sbox = [box[0], box[1] + _SS, box[2], box[3] + _SS]
        d.rounded_rectangle(sbox, radius * _SS, fill=(0, 0, 0, 22))
    if outline:
        d.rounded_rectangle(box, radius * _SS, fill=_rgba(outline))
        ow = outline_width * _SS
        box = [box[0] + ow, box[1] + ow, box[2] - ow, box[3] - ow]
        d.rounded_rectangle(box, max(radius * _SS - ow, 0), fill=_rgba(fill))
    else:
        d.rounded_rectangle(box, radius * _SS, fill=_rgba(fill))
    return big.resize((w, h), Image.LANCZOS)


def _photo(name, img):
    photo = ImageTk.PhotoImage(img)
    _images[name] = photo
    return photo


def _checkmark(size, color):
    big = Image.new("RGBA", (size * _SS, size * _SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    s = size * _SS
    d.line([(s * 0.26, s * 0.52), (s * 0.43, s * 0.68), (s * 0.74, s * 0.33)],
           fill=_rgba(color), width=int(s * 0.12), joint="curve")
    return big.resize((size, size), Image.LANCZOS)


def _chevron(w, h, color):
    big = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    W, H = w * _SS, h * _SS
    d.line([(W * 0.2, H * 0.35), (W * 0.5, H * 0.65), (W * 0.8, H * 0.35)],
           fill=_rgba(color), width=int(1.6 * _SS), joint="curve")
    return big.resize((w, h), Image.LANCZOS)


def _pad(img, left=0, top=0, right=0, bottom=0):
    out = Image.new("RGBA", (img.width + left + right, img.height + top + bottom), (0, 0, 0, 0))
    out.paste(img, (left, top), img)
    return out


def _circle(size, fill, outline=None, shadow=False):
    pad = 2 if shadow else 0
    big = Image.new("RGBA", ((size + pad * 2) * _SS, (size + pad * 2) * _SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    o = pad * _SS
    S = size * _SS
    if shadow:
        sh = Image.new("RGBA", big.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).ellipse([o, o + _SS, o + S - 1, o + S - 1 + _SS], fill=(0, 0, 0, 60))
        big = Image.alpha_composite(big, sh.filter(ImageFilter.GaussianBlur(_SS * 1.2)))
        d = ImageDraw.Draw(big)
    if outline:
        d.ellipse([o, o, o + S - 1, o + S - 1], fill=_rgba(outline))
        d.ellipse([o + _SS, o + _SS, o + S - 1 - _SS, o + S - 1 - _SS], fill=_rgba(fill))
    else:
        d.ellipse([o, o, o + S - 1, o + S - 1], fill=_rgba(fill))
    return big.resize((size + pad * 2, size + pad * 2), Image.LANCZOS)


# --------------------------------------------------------------------------
# ttk elements

def _make_elements(style):
    """Create the image elements once per process (element names are
    global to the theme and can't be re-created)."""
    global _elements_made
    if _elements_made or not HAVE_PIL:
        return
    _elements_made = True

    # Push buttons: white, hairline border, soft shadow; 6px radius.
    bw, bh, br = 160, 26, 6
    b_normal = _photo("btn", _rounded(bw, bh, br, "#ffffff", "#d1d1d6", shadow=True, inset=(0, 0, 0, 1)))
    b_hover = _photo("btn_h", _rounded(bw, bh, br, "#fafafa", "#c7c7cc", shadow=True, inset=(0, 0, 0, 1)))
    b_press = _photo("btn_p", _rounded(bw, bh, br, "#e8e8ed", "#c7c7cc", shadow=True, inset=(0, 0, 0, 1)))
    b_dis = _photo("btn_d", _rounded(bw, bh, br, "#f7f7f9", "#e3e3e8", inset=(0, 0, 0, 1)))
    style.element_create(
        "Apple.Button.border", "image", b_normal,
        ("disabled", b_dis), ("pressed", b_press), ("active", b_hover),
        border=(8, 8, 8, 9), padding=(8, 5, 8, 6), width=20, height=26, sticky="nsew",
    )

    a_normal = _photo("abtn", _rounded(bw, bh, br, ACCENT, None, shadow=True, inset=(0, 0, 0, 1)))
    a_hover = _photo("abtn_h", _rounded(bw, bh, br, "#2a7ff3", None, shadow=True, inset=(0, 0, 0, 1)))
    a_press = _photo("abtn_p", _rounded(bw, bh, br, ACCENT_PRESSED, None, shadow=True, inset=(0, 0, 0, 1)))
    style.element_create(
        "Apple.AccentButton.border", "image", a_normal,
        ("disabled", b_dis), ("pressed", a_press), ("active", a_hover),
        border=(8, 8, 8, 9), padding=(8, 5, 8, 6), width=20, height=26, sticky="nsew",
    )

    # Text fields: rounded white field, blue focus ring.
    e_normal = _photo("entry", _rounded(240, 26, 6, "#ffffff", "#d1d1d6", inset=(1, 1, 1, 1)))
    e_focus = _photo("entry_f", _rounded(240, 26, 7, "#ffffff", "#7fb0f7", outline_width=2))
    e_dis = _photo("entry_d", _rounded(240, 26, 6, "#f7f7f9", "#e3e3e8", inset=(1, 1, 1, 1)))
    style.element_create(
        "Apple.Entry.field", "image", e_normal,
        ("disabled", e_dis), ("focus", e_focus),
        border=(8, 8, 8, 8), padding=(6, 4, 6, 4), width=20, height=26, sticky="nsew",
    )

    # Slim overlay-style scrollbar thumb.
    t_normal = _photo("thumb", _rounded(12, 200, 4, "#c7c7cc", inset=(3, 2, 3, 2)))
    t_hover = _photo("thumb_h", _rounded(12, 200, 4, "#a1a1a6", inset=(3, 2, 3, 2)))
    style.element_create(
        "Apple.Vertical.thumb", "image", t_normal,
        ("pressed", t_hover), ("active", t_hover),
        border=(0, 8, 0, 8), width=12, height=20, sticky="nsew",
    )
    th_normal = _photo("hthumb", _rounded(200, 12, 4, "#c7c7cc", inset=(2, 3, 2, 3)))
    th_hover = _photo("hthumb_h", _rounded(200, 12, 4, "#a1a1a6", inset=(2, 3, 2, 3)))
    style.element_create(
        "Apple.Horizontal.thumb", "image", th_normal,
        ("pressed", th_hover), ("active", th_hover),
        border=(8, 0, 8, 0), width=20, height=12, sticky="nsew",
    )

    # Pill progress bar.
    p_trough = _photo("ptrough", _rounded(240, 8, 4, "#e5e5ea"))
    p_bar = _photo("pbar", _rounded(240, 8, 4, ACCENT))
    style.element_create("Apple.Progress.trough", "image", p_trough, border=(4, 4, 4, 4), width=20, height=8, sticky="ew")
    style.element_create("Apple.Progress.pbar", "image", p_bar, border=(4, 4, 4, 4), width=8, height=8, sticky="nsew")

    # Checkbox / radio.
    cb_off = _photo("cb_off", _pad(_rounded(16, 16, 4, "#ffffff", "#c7c7cc"), right=6))
    cb_on = _photo("cb_on", _pad(Image.alpha_composite(_rounded(16, 16, 4, ACCENT), _checkmark(16, "#ffffff")), right=6))
    cb_dis = _photo("cb_dis", _pad(_rounded(16, 16, 4, "#f2f2f5", "#e3e3e8"), right=6))
    style.element_create(
        "Apple.Checkbutton.indicator", "image", cb_off,
        ("disabled", cb_dis), ("selected", cb_on), sticky="",
    )
    rb_off = _photo("rb_off", _pad(_circle(16, "#ffffff", "#c7c7cc"), right=6))
    dot = _pad(_circle(6, "#ffffff"), 5, 5, 5, 5)
    rb_on = _photo("rb_on", _pad(Image.alpha_composite(_circle(16, ACCENT), dot), right=6))
    style.element_create(
        "Apple.Radiobutton.indicator", "image", rb_off,
        ("selected", rb_on), sticky="",
    )

    # Slider: thin track, round white knob with a soft shadow.
    s_trough = _photo("strough", _pad(_rounded(240, 4, 2, "#d1d1d6"), top=8, bottom=8))
    s_knob = _photo("sknob", _circle(18, "#ffffff", "#d1d1d6", shadow=True))
    s_knob_p = _photo("sknob_p", _circle(18, "#f2f2f5", "#c7c7cc", shadow=True))
    style.element_create("Apple.Scale.trough", "image", s_trough, border=(3, 9, 3, 9), width=20, height=20, sticky="ew")
    style.element_create("Apple.Scale.slider", "image", s_knob, ("pressed", s_knob_p), sticky="")

    # Group boxes become rounded cards.
    # Large source image: ttk tiles the image's middle across the whole
    # element, so a tiny middle on a big settings card meant thousands
    # of tiny copies per redraw (that's what made Settings lag).
    card = _photo("card", _rounded(400, 400, 10, CONTENT, SEPARATOR))
    style.element_create("Apple.Card.border", "image", card, border=(11, 11, 11, 11),
                         padding=(4, 4, 4, 4), width=24, height=24, sticky="nsew")

    # Graphite device card, like the front of an iPod classic: a soft
    # top-to-bottom gradient with a faint highlight edge.
    grad = Image.new("RGBA", (400, 400))
    gd = ImageDraw.Draw(grad)
    top, bottom = _rgba(DARK_CARD_TOP)[:3], _rgba(DARK_CARD_BOTTOM)[:3]
    for y in range(400):
        t = y / 399
        gd.line([(0, y), (399, y)], fill=tuple(int(a * (1 - t) + b * t) for a, b in zip(top, bottom)) + (255,))
    dark = _rounded(400, 400, 12, "#4a4a4e")  # whole shape in the edge color...
    inner = _rounded(400, 400, 11, "#ffffff", inset=(1, 1, 1, 1)).getchannel("A")
    dark.paste(grad, (0, 0), inner)            # ...then the gradient inside it
    dark_card = _photo("dcard", dark)
    style.element_create("Apple.DarkCard.border", "image", dark_card, border=(13, 13, 13, 13),
                         padding=(4, 4, 4, 4), width=28, height=28, sticky="nsew")

    # Segmented control: a recessed gray track, the selected segment a
    # raised white pill.
    seg_track = _photo("segtrack", _rounded(400, 40, 9, SEG_TRACK))
    style.element_create("Apple.SegTrack.border", "image", seg_track, border=(10, 10, 10, 10),
                         width=24, height=24, sticky="nsew")
    seg_on = _photo("segon", _rounded(200, 30, 7, "#ffffff", "#d8d8dd", shadow=True, inset=(0, 0, 0, 1)))
    style.element_create("Apple.SegOn.border", "image", seg_on, border=(8, 8, 8, 9),
                         padding=(14, 4, 14, 5), width=20, height=24, sticky="nsew")

    chev = _photo("chev", _pad(_chevron(10, 6, SECONDARY), left=6))
    style.element_create("Apple.Menubutton.indicator", "image", chev, sticky="")


def _install_layouts(style):
    if not HAVE_PIL:
        return
    button_inner = [("Button.padding", {"sticky": "nsew", "children": [
        ("Button.label", {"sticky": "nsew"})]})]
    style.layout("TButton", [("Apple.Button.border", {"sticky": "nsew", "children": button_inner})])
    style.layout("Accent.TButton", [("Apple.AccentButton.border", {"sticky": "nsew", "children": button_inner})])

    style.layout("TMenubutton", [("Apple.Button.border", {"sticky": "nsew", "children": [
        ("Menubutton.padding", {"sticky": "nsew", "children": [
            ("Apple.Menubutton.indicator", {"side": "right", "sticky": ""}),
            ("Menubutton.label", {"side": "left", "expand": "1", "sticky": "w"}),
        ]})]})])

    style.layout("TEntry", [("Apple.Entry.field", {"sticky": "nsew", "children": [
        ("Entry.padding", {"sticky": "nsew", "children": [
            ("Entry.textarea", {"sticky": "nsew"})]})]})])

    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Apple.Vertical.thumb", {"expand": "1", "sticky": "nsew"})]})])
    style.layout("Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {"sticky": "ew", "children": [
        ("Apple.Horizontal.thumb", {"expand": "1", "sticky": "nsew"})]})])

    style.layout("Horizontal.TProgressbar", [("Apple.Progress.trough", {"sticky": "ew", "children": [
        ("Apple.Progress.pbar", {"side": "left", "sticky": "ns"})]})])

    style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
        ("Apple.Checkbutton.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.label", {"side": "left", "sticky": "nsew"})]})])
    style.layout("TRadiobutton", [("Radiobutton.padding", {"sticky": "nsew", "children": [
        ("Apple.Radiobutton.indicator", {"side": "left", "sticky": ""}),
        ("Radiobutton.label", {"side": "left", "sticky": "nsew"})]})])

    style.layout("Horizontal.TScale", [("Apple.Scale.trough", {"sticky": "ew", "children": [
        ("Apple.Scale.slider", {"side": "left", "sticky": ""})]})])

    style.layout("TLabelframe", [("Apple.Card.border", {"sticky": "nsew"})])
    style.layout("Card.TFrame", [("Apple.Card.border", {"sticky": "nsew"})])
    style.layout("DarkCard.TFrame", [("Apple.DarkCard.border", {"sticky": "nsew"})])
    style.layout("SegTrack.TFrame", [("Apple.SegTrack.border", {"sticky": "nsew"})])
    style.layout("Segment.TButton", [("Button.padding", {"sticky": "nsew", "children": [
        ("Button.label", {"sticky": "nsew"})]})])
    style.layout("SegmentOn.TButton", [("Apple.SegOn.border", {"sticky": "nsew", "children": [
        ("Button.label", {"sticky": "nsew"})]})])
    style.layout("SidebarLink.TButton", [("Button.padding", {"sticky": "nsew", "children": [
        ("Button.label", {"sticky": "w"})]})])

    # Rows without the dotted keyboard-focus rectangle.
    style.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nsew", "children": [
        ("Treeitem.indicator", {"side": "left", "sticky": ""}),
        ("Treeitem.image", {"side": "left", "sticky": ""}),
        ("Treeitem.text", {"side": "left", "sticky": ""})]})])


def apply(root, style):
    """Configure the (already selected) clam theme for the Apple look."""
    _make_elements(style)
    _install_layouts(style)

    style.configure(".", background=CONTENT, foreground=TEXT, troughcolor=CONTENT,
                    bordercolor=SEPARATOR, lightcolor=CONTENT, darkcolor=CONTENT,
                    focuscolor=ACCENT, selectbackground=ACCENT, selectforeground="#ffffff",
                    font="TkDefaultFont")
    style.configure("TFrame", background=CONTENT)
    style.configure("Content.TFrame", background=CONTENT)
    style.configure("Chrome.TFrame", background=CHROME)
    style.configure("TLabel", background=CONTENT, foreground=TEXT)
    style.configure("Content.TLabel", background=CONTENT)
    style.configure("Chrome.TLabel", background=CHROME, foreground=SECONDARY)
    style.configure("Secondary.TLabel", background=CONTENT, foreground=SECONDARY)

    style.configure("TButton", padding=(2, 0), width=-6, foreground=TEXT, anchor="center")
    style.map("TButton", foreground=[("disabled", "#b0b0b5")], background=[])
    style.configure("Accent.TButton", padding=(2, 0), width=-6, foreground="#ffffff", anchor="center")
    style.map("Accent.TButton", foreground=[("disabled", "#b0b0b5")])
    style.configure("TMenubutton", padding=(2, 0), foreground=TEXT)
    style.map("TMenubutton", background=[])

    style.configure("TEntry", padding=(2, 0), fieldbackground="#ffffff", foreground=TEXT,
                    insertcolor=TEXT, insertwidth=1)
    style.configure("TCombobox", padding=(6, 3), fieldbackground="#ffffff", background="#ffffff",
                    arrowcolor=SECONDARY, bordercolor="#d1d1d6", lightcolor="#ffffff", darkcolor="#ffffff")
    style.map("TCombobox", bordercolor=[("focus", "#7fb0f7")], fieldbackground=[("readonly", "#ffffff")])
    style.configure("TSpinbox", padding=(6, 3), fieldbackground="#ffffff", background="#ffffff",
                    arrowcolor=SECONDARY, bordercolor="#d1d1d6", lightcolor="#ffffff", darkcolor="#ffffff")
    style.map("TSpinbox", bordercolor=[("focus", "#7fb0f7")])

    style.configure("TCheckbutton", background=CONTENT, foreground=TEXT, padding=(0, 2))
    style.map("TCheckbutton", background=[("active", CONTENT)])
    style.configure("TRadiobutton", background=CONTENT, foreground=TEXT, padding=(0, 2))
    style.map("TRadiobutton", background=[("active", CONTENT)])
    style.configure("TScale", background=CONTENT)

    style.configure("Vertical.TScrollbar", troughcolor=CONTENT, background=CONTENT, borderwidth=0)
    style.configure("Horizontal.TScrollbar", troughcolor=CONTENT, background=CONTENT, borderwidth=0)
    style.configure("Horizontal.TProgressbar", background=CHROME)
    style.layout("Card.Horizontal.TProgressbar", style.layout("Horizontal.TProgressbar"))
    style.configure("Card.Horizontal.TProgressbar", background=CONTENT)

    style.configure("TLabelframe", background=CONTENT, labeloutside=True,
                    labelmargins=(4, 0, 0, 6), padding=10)
    style.configure("TLabelframe.Label", background=CONTENT, foreground=TEXT,
                    font=(FAMILY, 10, "bold"))
    style.configure("TSeparator", background=SEPARATOR)
    style.configure("Chrome.TSeparator", background="#d8d8dd")

    style.configure(
        "Treeview", background=CONTENT, fieldbackground=CONTENT, foreground=TEXT,
        rowheight=28, borderwidth=0, relief="flat", font="TkDefaultFont",
        bordercolor=CONTENT, lightcolor=CONTENT, darkcolor=CONTENT,
    )
    style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
    style.configure(
        "Treeview.Heading", background=CONTENT, foreground=SECONDARY, relief="flat",
        borderwidth=1, bordercolor=SEPARATOR, lightcolor=CONTENT, darkcolor=SEPARATOR,
        font=(FAMILY, 9, "bold"), padding=(6, 5),
    )
    style.map("Treeview.Heading", background=[("active", STRIPE), ("pressed", SEPARATOR)])
    style.configure(
        "Sidebar.Treeview", background=CHROME, fieldbackground=CHROME, foreground=TEXT,
        rowheight=30, borderwidth=0, font="TkDefaultFont", indent=12,
        bordercolor=CHROME, lightcolor=CHROME, darkcolor=CHROME,
    )
    # iOpenPod-style: the selected source is a pale blue pill with blue text.
    style.map("Sidebar.Treeview", background=[("selected", ACCENT_SOFT)],
              foreground=[("selected", ACCENT)])

    # Content-pane header (page title) and the sidebar's device card.
    style.configure("Header.TLabel", background=CONTENT, foreground=TEXT, font=(FAMILY, 17, "bold"))
    style.configure("Card.TFrame", background=CHROME)
    style.configure("CardInner.TFrame", background=CONTENT)
    # Buttons sitting on the dark device card: same look, but the
    # rounded corners blend into the card instead of showing white.
    style.layout("CardAccent.TButton", style.layout("Accent.TButton"))
    style.configure("CardAccent.TButton", padding=(2, 0), foreground="#ffffff", anchor="center",
                    background=DARK_CARD_MID)
    style.map("CardAccent.TButton", foreground=[("disabled", "#b0b0b5")])
    style.layout("CardPlain.TButton", style.layout("TButton"))
    style.configure("CardPlain.TButton", padding=(2, 0), foreground=TEXT, anchor="center",
                    background=DARK_CARD_MID)
    style.configure("HeaderSub.TLabel", background=CONTENT, foreground=SECONDARY, font=(FAMILY, 10))
    style.configure("Brand.TLabel", background=CHROME, foreground=TEXT, font=(FAMILY, 11, "bold"))
    style.configure("SegTrack.TFrame", background=CHROME)
    style.configure("Segment.TButton", background=SEG_TRACK, foreground=SECONDARY,
                    padding=(14, 4, 14, 5), anchor="center", font=(FAMILY, 10))
    style.map("Segment.TButton", foreground=[("active", TEXT)], background=[("active", SEG_TRACK)])
    style.configure("SegmentOn.TButton", background=SEG_TRACK, foreground=TEXT, anchor="center",
                    font=(FAMILY, 10, "bold"))
    style.map("SegmentOn.TButton", background=[("active", SEG_TRACK)])
    # The midnight-blue device card. Inner frames/labels use the card's mid
    # tone so they sit on the gradient without visible boxes.
    style.configure("DarkCard.TFrame", background=CHROME)
    style.configure("DarkCardInner.TFrame", background=DARK_CARD_MID)
    style.configure("DarkCard.TLabel", background=DARK_CARD_MID, foreground="#ffffff")
    style.configure("DarkCardTitle.TLabel", background=DARK_CARD_MID, foreground="#ffffff",
                    font=(FAMILY, 11, "bold"))
    style.configure("DarkCardSecondary.TLabel", background=DARK_CARD_MID, foreground="#a9bcd9",
                    font=(FAMILY, 9))
    style.configure("DarkCardSmall.TLabel", background=DARK_CARD_MID, foreground="#dbe5f3",
                    font=(FAMILY, 8))
    style.configure("DarkCardLink.TLabel", background=DARK_CARD_MID, foreground="#9fd0ff",
                    font=(FAMILY, 10))
    style.configure("DarkCardEject.TLabel", background=DARK_CARD_MID, foreground="#d1d1d6",
                    font=(FAMILY, 13))
    style.map("DarkCardEject.TLabel", foreground=[("active", "#ffffff")])
    for key, _label in CAPACITY_KINDS:
        style.configure(f"Legend{key}.TLabel", background=DARK_CARD_MID,
                        foreground=CAPACITY_COLORS[key], font=(FAMILY, 8))
    style.configure("Card.TLabel", background=CONTENT, foreground=TEXT)
    style.configure("CardTitle.TLabel", background=CONTENT, foreground=TEXT, font=(FAMILY, 11, "bold"))
    style.configure("CardSecondary.TLabel", background=CONTENT, foreground=SECONDARY, font=(FAMILY, 9))
    style.configure("CardSmall.TLabel", background=CONTENT, foreground=TEXT, font=(FAMILY, 9))
    style.configure("CardLink.TLabel", background=CONTENT, foreground=ACCENT, font=(FAMILY, 10))
    style.configure("Icon.TButton", padding=(0, 0), width=2, foreground=TEXT, anchor="center")
    style.configure("SidebarLink.TButton", background=CHROME, foreground=TEXT, padding=(14, 8),
                    anchor="w", relief="flat", borderwidth=0)
    style.map("SidebarLink.TButton", background=[("pressed", SEPARATOR), ("active", "#e8e8ed")],
              foreground=[("active", TEXT)])
    root.configure(background=CHROME)


# --------------------------------------------------------------------------
# Motion

def _ease_out(t):
    return 1 - (1 - t) ** 3


_scroll_state = {}  # str(canvas) -> {"target": px, "job": after id}


def smooth_scroll(canvas, direction, enabled=True, step=None):
    """Glide a canvas by one wheel notch (direction -1 = up, +1 = down)
    instead of jumping a whole "unit". Further notches during a glide
    extend its target, so fast wheel spins feel continuous."""
    if not enabled:
        canvas.yview_scroll(direction, "units")
        return
    try:
        region = canvas.cget("scrollregion").split()
        total = float(region[3]) - float(region[1]) if len(region) == 4 else 0
    except (tk.TclError, ValueError):
        total = 0
    visible = canvas.winfo_height()
    if total <= visible:
        return
    key = str(canvas)
    state = _scroll_state.get(key)
    current = canvas.yview()[0] * total
    if state is None or state.get("job") is None:
        state = {"target": current, "job": None}
        _scroll_state[key] = state
    step = step or max(60, min(140, visible // 5))
    state["target"] = max(0.0, min(total - visible, state["target"] + direction * step))

    def frame():
        try:
            region = canvas.cget("scrollregion").split()
            total_now = float(region[3]) - float(region[1])
            pos = canvas.yview()[0] * total_now
        except (tk.TclError, ValueError, IndexError):
            state["job"] = None
            return
        target = max(0.0, min(total_now - canvas.winfo_height(), state["target"]))
        diff = target - pos
        if abs(diff) < 1:
            canvas.yview_moveto(target / total_now if total_now else 0)
            state["job"] = None
            return
        canvas.yview_moveto((pos + diff * 0.28) / total_now)
        state["job"] = canvas.after(12, frame)

    if state["job"] is None:
        frame()


class SmoothProgressbar(ttk.Progressbar):
    """A determinate progress bar that eases toward each new value
    rather than jumping. Resets (value going down) are instant."""

    animate = True

    def configure(self, cnf=None, **kw):
        target = kw.pop("value", None)
        result = None
        if cnf is not None or kw or target is None:
            result = super().configure(cnf, **kw)
        if target is not None:
            self._ease_to(float(target))
        return result

    config = configure

    def _ease_to(self, target):
        job = getattr(self, "_ease_job", None)
        if job is not None:
            self.after_cancel(job)
            self._ease_job = None
        try:
            current = float(self.cget("value"))
            determinate = str(self.cget("mode")) == "determinate"
        except tk.TclError:
            return
        if not self.animate or not determinate or target <= current:
            super().configure(value=target)
            return
        start, steps = current, 10

        def tick(i=1):
            value = start + (target - start) * _ease_out(i / steps)
            try:
                super(SmoothProgressbar, self).configure(value=value)
            except tk.TclError:
                return
            if i < steps:
                self._ease_job = self.after(16, tick, i + 1)
            else:
                self._ease_job = None

        tick()


_fades_running = 0
_MAX_FADES = 24


def fade_in_image(label, start_color, pil_img, final_photo, bg=CONTENT):
    """Cross-fade a label from a flat color swatch to pil_img, ending on
    final_photo. Skipped (instant swap) when many fades are already
    running, e.g. a big library's covers all arriving at once."""
    global _fades_running
    if not HAVE_PIL or _fades_running >= _MAX_FADES:
        label.configure(image=final_photo)
        label.image = final_photo
        return
    try:
        src = pil_img.convert("RGBA")
        base = Image.new("RGBA", src.size, _rgba(start_color))
        if src.getchannel("A").getextrema()[0] < 255:
            # Keep rounded corners transparent throughout the fade.
            base.putalpha(src.getchannel("A"))
    except Exception:
        label.configure(image=final_photo)
        label.image = final_photo
        return
    _fades_running += 1
    steps = 6

    def tick(i=1):
        global _fades_running
        if not label.winfo_exists():
            _fades_running -= 1
            return
        if i >= steps:
            label.configure(image=final_photo)
            label.image = final_photo
            _fades_running -= 1
            return
        frame = ImageTk.PhotoImage(Image.blend(base, src, _ease_out(i / steps)))
        label.configure(image=frame)
        label.image = frame
        label.after(22, tick, i + 1)

    tick()


def fade_in_window(root, duration_ms=220):
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        return
    steps = 11

    def tick(i=1):
        try:
            root.attributes("-alpha", 1.0 if i >= steps else _ease_out(i / steps))
        except tk.TclError:
            return
        if i < steps:
            root.after(duration_ms // steps, tick, i + 1)

    root.after(30, tick)


_corner_masks = {}


def _corner_layers(w, h, radius, outline):
    """The antialiased corner mask and hairline edge for one size, drawn
    once and reused - every album cover is the same size, and redrawing
    them supersampled per cover was most of the cost of a grid load."""
    key = (w, h, radius, outline)
    layers = _corner_masks.get(key)
    if layers is None:
        mask = Image.new("L", (w * _SS, h * _SS), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * _SS - 1, h * _SS - 1], radius * _SS, fill=255)
        mask = mask.resize((w, h), Image.LANCZOS)
        edge = None
        if outline:
            edge = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
            a = int(outline[7:9], 16) if len(outline) == 9 else 30
            ImageDraw.Draw(edge).rounded_rectangle(
                [0, 0, w * _SS - 1, h * _SS - 1], radius * _SS,
                outline=_rgba(outline[:7], a), width=_SS,
            )
            edge = edge.resize((w, h), Image.LANCZOS)
        layers = (mask, edge)
        if len(_corner_masks) > 32:
            _corner_masks.clear()
        _corner_masks[key] = layers
    return layers


def round_corners(pil_img, radius=8, outline="#00000014"):
    """RGBA copy of pil_img with rounded, antialiased corners and a
    faint hairline edge so pale covers still read against white."""
    if not HAVE_PIL:
        return pil_img
    img = pil_img.convert("RGBA")
    mask, edge = _corner_layers(img.width, img.height, radius, outline)
    img.putalpha(mask)
    if edge is not None:
        img = Image.alpha_composite(img, edge)
    return img


def rounded_swatch(color, w, h, radius=8):
    if not HAVE_PIL:
        return None
    return round_corners(Image.new("RGB", (w, h), color), radius, outline=None)


def ipod_icon_photo(height=46, silver=False):
    """A small drawn iPod classic (body, screen, click wheel) for the
    device card - silver for use on the dark card. None without Pillow."""
    if not HAVE_PIL:
        return None
    if silver:
        body, wheel, center = "#e5e5ea", "#ffffff", "#c7c7cc"
    else:
        body, wheel, center = "#2c2c2e", "#d1d1d6", "#2c2c2e"
    w, h = int(height * 0.64), height
    S = _SS
    big = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([0, 0, w * S - 1, h * S - 1], int(w * 0.16) * S, fill=_rgba(body))
    m = int(w * 0.12) * S
    screen_h = int(h * 0.36) * S
    d.rounded_rectangle([m, m, w * S - m, m + screen_h], 2 * S, fill=_rgba("#9fb3c8"))
    r = int(w * 0.33) * S
    cx, cy = w * S // 2, int(h * 0.71) * S
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_rgba(wheel))
    r2 = int(r * 0.38)
    d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=_rgba(center))
    img = big.resize((w, h), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def tint_for(pil_img, strength=0.26):
    """A pale card color picked from a cover's average color, the way
    iOpenPod tints each album card to match its art."""
    try:
        r, g, b = pil_img.convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
    except Exception:
        return None
    mix = lambda c: int(255 * (1 - strength) + c * strength)
    return "#%02x%02x%02x" % (mix(r), mix(g), mix(b))


def tint_for_color(color, strength=0.26):
    if not HAVE_PIL:
        return CHROME
    return tint_for(Image.new("RGB", (1, 1), color), strength) or CHROME


def capacity_bar_photo(width, sizes, total, height=10, bg=DARK_CARD_MID):
    """An iTunes-style capacity bar: one rounded pill split into colored
    segments (audio / video / other / free) sized by bytes."""
    if not HAVE_PIL or total <= 0:
        return None
    S = _SS
    W, H = width * S, height * S
    bar = Image.new("RGBA", (W, H), _rgba(bg))
    d = ImageDraw.Draw(bar)
    x = 0.0
    for key, _label in CAPACITY_KINDS:
        seg = W * (sizes.get(key, 0) / total)
        if seg <= 0:
            continue
        d.rectangle([int(x), 0, int(x + seg) + 1, H], fill=_rgba(CAPACITY_COLORS[key]))
        # A thin dark gap between segments, like the iTunes bar.
        x += seg
        if x < W - 1:
            d.rectangle([int(x), 0, int(x) + S, H], fill=_rgba(bg))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], H // 2, fill=255)
    out = Image.new("RGBA", (W, H), _rgba(bg))
    out.paste(bar, (0, 0), mask)
    # Glossy top highlight.
    gloss = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).rounded_rectangle([0, 0, W - 1, H // 2], H // 2, fill=(255, 255, 255, 40))
    gloss_masked = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gloss_masked.paste(gloss, (0, 0), mask)
    out = Image.alpha_composite(out, gloss_masked)
    return ImageTk.PhotoImage(out.resize((width, height), Image.LANCZOS))


def add_reflection(pil_img, height_frac=0.22, strength=0.32):
    """Cover Flow-style reflection: the bottom of the image mirrored
    underneath, fading out. Returns a taller RGBA image (transparent
    where the reflection has faded, so the card color shows through)."""
    if not HAVE_PIL:
        return pil_img
    img = pil_img.convert("RGBA")
    w, h = img.size
    rh = max(int(h * height_frac), 1)
    refl = img.crop((0, h - rh, w, h)).transpose(Image.FLIP_TOP_BOTTOM)
    fade = Image.new("L", (1, rh))
    for y in range(rh):
        fade.putpixel((0, y), int(255 * strength * (1 - y / rh) ** 1.6))
    fade = fade.resize((w, rh))
    alpha = Image.composite(fade, Image.new("L", (w, rh), 0), refl.getchannel("A"))
    refl.putalpha(alpha)
    gap = 2
    out = Image.new("RGBA", (w, h + gap + rh), (0, 0, 0, 0))
    out.paste(img, (0, 0))
    out.paste(refl, (0, h + gap))
    return out

