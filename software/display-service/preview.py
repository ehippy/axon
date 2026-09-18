#!/usr/bin/env python3
"""Render any Axon HUD screen to a PNG, without touching display-device or the
Whisplay hardware at all - same drawing code, just saved to a file instead
of pushed to the panel. For iterating on art while away from the device.

Usage:
    python3 preview.py idle
    python3 preview.py idle --art-dir ./art-drafts --out idle_v2.png
    python3 preview.py --list

--art-dir points hud.load_art() at a local folder instead of the on-device
/home/pi/axon-assets - drop candidate idle.png there and this renders it in
full screen context (title, status text and all), real color now (the
Whisplay LCD isn't 1-bit like the old e-ink panel was).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("screen", nargs="?", help="screen name (idle, boot, shutdown)")
    parser.add_argument("--art-dir", default=None, help="local folder to load art from instead of /home/pi/axon-assets")
    parser.add_argument("--out", default=None, help="output PNG path (default: <screen>_preview.png)")
    parser.add_argument("--scale", type=int, default=4, help="upscale factor for visibility (panel is 240x280px)")
    parser.add_argument("--battery", type=int, default=72, metavar="PCT", help="fake battery percent (no real PiSugar off-device) - default 72")
    parser.add_argument("--charging", action="store_true", help="show the fake battery as charging")
    parser.add_argument("--logical", action="store_true", help="save the pre-rotation logical composition instead of what actually appears on the physical panel")
    parser.add_argument("--list", action="store_true", help="list available screen names and exit")
    args = parser.parse_args()

    if args.art_dir:
        os.environ["AXON_ASSETS_DIR"] = os.path.abspath(args.art_dir)

    import hud
    import screens

    # No real PiSugar to query off-device - fake get_battery_status() so the
    # battery icon (and anything else reading it) has something to show.
    screens.get_battery_status = lambda: {
        "percent": args.battery, "charging": args.charging, "voltage": 3.7 + args.battery / 200,
    }

    if args.list or not args.screen:
        print("Available screens:", ", ".join(screens.RENDERERS.keys()))
        if not args.screen:
            return
        return

    if args.screen not in screens.RENDERERS:
        print(f"Unknown screen {args.screen!r}. Available: {', '.join(screens.RENDERERS.keys())}", file=sys.stderr)
        sys.exit(1)

    # Fake state good enough to render something representative - real
    # values on-device come from the daemon's live state dict.
    import time as _time
    fake_state = {
        "screen": args.screen,
        "hostname": "axon",
        "agents": {
            "hermes": {"state": "running", "task": "read_file, grep", "ts": _time.time()},
            "workstation": {"state": "running", "task": "editing screens.py", "ts": _time.time() - 190},
            "broker-host": {"state": "idle", "task": "awaiting input", "ts": _time.time() - 4200},
        },
    }

    image, draw = hud.new_canvas()
    screens.RENDERERS[args.screen](image, draw, fake_state)

    if not args.logical:
        # What actually appears on the physical panel - rotated, same as
        # the real daemon does right before calling board.draw_image().
        image = hud.rotate_for_display(image)

    out_path = args.out or f"{args.screen}_preview.png"
    scaled = image.resize((image.width * args.scale, image.height * args.scale), resample=0)  # NEAREST - keep hard pixel edges
    scaled.save(out_path)
    print(f"Wrote {out_path} ({scaled.width}x{scaled.height}, {args.scale}x scale)")
    if args.art_dir:
        print(f"Loaded art from {os.environ['AXON_ASSETS_DIR']}")


if __name__ == "__main__":
    main()
