"""Runs inside the RotorHazard container: work the OFT Timers settings panel over socket.io.

    panel.py add            press Find Timers, then add every Timer found
    panel.py expect TEXT..  press Find Timers, then check the panel text has each TEXT
"""
import sys
import time

import socketio

sio = socketio.Client()
panels, messages = [], []


@sio.on("*")
def anything(event, data=None):
    if event == "ui":
        panels.extend(p for p in data["panels"] if p["panel"]["name"] == "rh_oft")
    elif event == "priority_message":
        messages.append(data["message"])


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        print("      panel text:\n" + text())
        sys.exit(1)


def press(button):
    sio.emit("dispatch_event", {"namespace": "quickbutton", "id": button})
    time.sleep(3.5 if button == "rh_oft_find" else 1)  # a search listens for 2 s


def text():
    return panels[-1]["markdowns"][0]["desc"] if panels else ""


def options(field):
    setting = next(s for s in panels[-1]["settings"] if s["name"] == field)
    return [o["value"] for o in setting["options"] if o["value"]]


sio.connect("http://localhost:5000", wait_timeout=10)
time.sleep(2)
press("rh_oft_find")
check(panels, "the settings page is sent the OFT Timers panel")

if sys.argv[1] == "add":
    found = options("rh_oft_found")
    check(len(found) == 2, "Find Timers lists the Timers on the network: %s" % found)
    check("None yet" in text(), "none is in use before the user adds it")
    for timer in found:
        sio.emit("set_option", {"option": "rh_oft_found", "value": timer})
        time.sleep(0.5)
        press("rh_oft_add")
        check(messages and messages[-1] == "Added Timer %s. Restart RotorHazard to apply." % timer, "added %s" % timer)
    check(options("rh_oft_found") == [] and sorted(options("rh_oft_remove")) == sorted(found), "both are now listed as in use")
else:
    for want in sys.argv[2:]:
        check(want in text(), "panel shows: %s" % want)
sio.disconnect()
