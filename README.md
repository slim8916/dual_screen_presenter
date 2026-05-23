# Sync PDF Presenter

Linux command-line tool for synchronized dual-screen PDF presentations.

It opens two PDF files with Evince:

- a speaker notes PDF on your laptop screen
- a clean slides PDF on the projector or external monitor

The terminal controller keeps both windows synchronized. When you go to the next or previous page, both PDFs move together.

In presentation mode, the script opens each PDF with Evince, moves the windows to the selected monitors, then sends `F5` so Evince enters real presentation/slideshow mode. The terminal controller keeps keyboard focus so `n`, arrows, PageUp, and PageDown stay synchronized.
Mouse/touch clicks on either presentation screen are also synchronized: the script catches the click before Evince handles it and sends the same page action to both PDFs.

## Requirements

This MVP uses Evince as the PDF viewer backend and controls windows with X11 tools.

On Debian/Ubuntu:

```bash
sudo apt install evince xdotool wmctrl x11-xserver-utils
```

Python 3.10 or newer is required.

## Usage

List active monitors:

```bash
python3 sync_pdf_presenter.py --list-monitors
```

Start a presentation:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1
```

The notes and slides must be two different `.pdf` files.

Start at a specific page:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1 \
  --start-page 5
```

Wait longer for slow Evince startup:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1 \
  --window-timeout 40
```

Use Evince fullscreen mode directly if presentation mode is not reliable on your desktop:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1 \
  --evince-mode fullscreen
```

`--evince-mode fullscreen` uses ordinary fullscreen. For slideshow/presentation mode, omit `--evince-mode` or pass `--evince-mode presentation`.

Swap the notes/slides monitor assignments without rewriting the command:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1 \
  --swap-screens
```

Close Evince windows when the controller exits:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1 \
  --close-on-exit
```

Mouse/touch clicks are synchronized by default. Evince does not receive the click directly; the script catches the click and sends the same page action to both PDFs. Left click/tap and scroll down go to the next page; right click and scroll up go to the previous page.

Validate the setup without opening Evince:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1 \
  --dry-run
```

`--dry-run` requires only Evince and `xrandr`; it does not need `xdotool` or `wmctrl`.

Show debug output:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1 \
  --debug
```

## Controls

Use the terminal where the script is running:

- next page: `n`, Right arrow, PageDown, Space
- previous page: `p`, Left arrow, PageUp, BackSpace
- help: `h`
- quit controller: `q`

Mouse/touch controls are synchronized with the same controller path as the keyboard controls. Left click/tap and scroll down go to the next page; right click and scroll up go to the previous page.

Quitting the controller leaves the Evince windows open.

## Wayland Note

This tool works best under X11/Xorg because `xdotool` and `wmctrl` are X11 tools.

Check your session type:

```bash
echo $XDG_SESSION_TYPE
```

If it prints `wayland`, use a GNOME on Xorg session for best results. You can force the script to continue under Wayland:

```bash
python3 sync_pdf_presenter.py \
  --notes notes.pdf --notes-screen eDP-1 \
  --slides presentation.pdf --slides-screen HDMI-1 \
  --force-wayland
```

Window movement and key synchronization may still fail under Wayland.

## Troubleshooting

List monitors directly:

```bash
xrandr --query
```

Debug Evince window IDs:

```bash
xdotool search --onlyvisible --class evince
wmctrl -lxp
```

If the script cannot find or move windows:

- make sure you are running an X11 session
- make sure `$DISPLAY` is set
- make sure both PDFs exist
- run with `--debug`
- try `--dry-run` first
- check that the monitor names match `python3 sync_pdf_presenter.py --list-monitors`
- if Evince opens "Recent Documents", rerun with `--debug`; the script should no longer treat that as a valid PDF window and should retry with safer launch modes
- if the default launch order fails, try `--evince-mode fullscreen`
- the script sends `F5` after moving the windows so Evince enters slideshow mode

Manual Evince checks:

```bash
evince --new-window --presentation /absolute/path/to/notes.pdf
evince --presentation /absolute/path/to/notes.pdf
evince --new-window --fullscreen /absolute/path/to/notes.pdf
evince --fullscreen /absolute/path/to/notes.pdf
evince --new-window /absolute/path/to/notes.pdf
evince /absolute/path/to/notes.pdf
```

For `--start-page 5`, the script opens both PDFs normally, enters slideshow mode, then sends four synchronized `Page_Down` actions to both PDFs. This avoids Evince command-line page options that are unreliable with presentation startup on some desktops.

## Development Checks

Run the lightweight tests:

```bash
python3 -m unittest -v
```

## MVP Scope

The first version intentionally depends on Evince instead of implementing a custom PDF renderer. This keeps the code simpler and easier to publish.

Possible future additions:

- support for Okular or Zathura through a `--viewer` option
- offsets between notes and slides page numbers
- config file support
