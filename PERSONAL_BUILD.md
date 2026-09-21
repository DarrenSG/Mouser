# Personal macOS build

This branch is maintained for DarrenSG's own use. It starts at upstream
`v3.7.3` and identifies itself as `3.7.3+dpiwake.1`. No upstream PR is planned.

## DPI recovery

A Logitech mouse can sleep and reset its sensor DPI while its USB receiver
stays connected. The existing reconnect callback never runs in that case.

The macOS event tap now signals physical pointer activity to the HID listener.
After a gap of at least 30 seconds, the listener checks sensor DPI approximately
0.5, 2, and 5 seconds later (subject to its existing read timeout). A mismatch
is restored to the latest requested DPI and read back. Later checks cover a
delayed firmware reset. Normal movement does not keep scheduling checks, and
there is no periodic HID polling while idle.

All USB commands execute on the existing HID listener thread. The event tap
does not block on device I/O, recovery does not reuse the UI command mailbox,
and queued manual changes take precedence. Disconnect cancels outstanding
checks but preserves the desired DPI. No SmartShift rewrite is needed.

Quartz reports pointer events from all pointing devices, so a trackpad can
also trigger a bounded check of the connected Logitech mouse. Software-injected
events do not trigger recovery.

## Build and verify

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-macos-personal.lock
.venv/bin/python -m unittest discover -s tests -p test_dpi_wake.py
./build_macos_app.sh
codesign --verify --deep --strict dist/Mouser.app
```

Quit the installed Mouser before replacing `/Applications/Mouser.app`. Keep
the old bundle archived for rollback; never launch it alongside the new build.
The bundle identifier and installation path are unchanged, preserving the
existing settings and login item. macOS may require Accessibility access to
be granted again after rebuilding. Disable upstream update checks in settings
to avoid update prompts for builds that do not contain this patch.

To validate on hardware, leave the mouse untouched for at least 30 seconds,
then move it. The application log should contain `[DPIWake] Verified 4000 DPI`
(or your chosen DPI), with a `Restoring DPI` entry if the sensor had reset.
Also test actual mouse sleep, power cycling, receiver reconnect, and remapped
buttons. Logs live at `~/Library/Logs/Mouser/mouser.log`.
