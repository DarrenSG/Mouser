# Personal macOS build

This branch is maintained for DarrenSG's own use. It starts at upstream
`v3.7.3` and identifies itself as `3.7.3+dpiwake.3`. No upstream PR is planned.

## DPI recovery

A Logitech mouse can sleep and reset its sensor DPI while its USB receiver
stays connected. The existing reconnect callback never runs in that case.

The macOS event tap signals physical pointer activity to the HID listener.
The listener checks DPI at most once per two seconds of activity and restores
saved intent on mismatch. A failed read does not prevent a restore write, and
further movement retries after failures without a three-attempt cutoff.
Individual recovery requests use a 500 ms timeout. Manual DPI requests retain
priority and their response mailbox is separate.

No minimum idle period is required. Pending activity expires after three
seconds, so there is no recurring idle polling. Pointer activity also wakes the
listener's software sleep state even if Bolt supplies no HID++ wake report.
All USB commands execute on the existing listener thread; the event tap never
waits for USB. Normal checks do not rewrite SmartShift.

Quartz reports pointer events from all pointing devices, so a trackpad can
also trigger a check of the connected Logitech mouse. Software-injected events
do not trigger recovery. Unchanged successful checks are quiet in the log.

The first build (`dpiwake.1`) failed a real power-cycle test: its finite retry
window expired during wake, and it required a new 30-second idle gap to rearm.
That build is superseded. The regression suite now covers quick power cycles,
late resets during movement, recovery after more than three failed checks,
failed reads with successful writes, and pointer-only wake after HID timeouts.

The third build also fixes receiver discovery: replies must match the requested
receiver slot, and discovery allows 1.5 seconds for the first response. A real
background-thread probe showed the first response arriving after about 900 ms,
exceeding the upstream 400 ms limit; the late reply could previously be accepted
for the next slot. Sensor reads also reject replies from other paired devices.
The combined recovery, HID, and mouse-hook suites pass (134 tests).

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

To validate on hardware, power the mouse off and back on using the same
sequence that reproduced the bug, then move it without changing General
Settings. Check pointer speed and the `[DPIWake]` restoration and sensor
read-back messages in `~/Library/Logs/Mouser/mouser.log`. A synthetic sensor
reset or a short idle check alone does not prove a real power-cycle fix.
Long sleep/wake is a separate validation case.
