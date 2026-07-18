#!/usr/bin/env python3
"""Phase 1 helper: create the ytmusicapi auth file for YOUR YouTube Music account.

You run this on your own machine. The credentials never leave it, and they are
never shared with Claude — Claude only ever sees the smoke-test *counts*, not the
auth file.

Two methods (browser is easiest for the proof; oauth is more durable):

  BROWSER  (recommended for the Phase 1 proof)
    Easiest is actually the built-in CLI:
        pip install ytmusicapi
        ytmusicapi browser        # writes browser.json, prompts for headers
    ...or use this script:
        1. Open https://music.youtube.com logged in.
        2. Dev tools -> Network -> filter "/browse".
        3. Click any POST to /browse -> copy the *request headers* as raw text.
        4. Run:  python auth_setup.py browser
           Paste headers, then EOF (Windows: Ctrl-Z then Enter; mac/Linux: Ctrl-D).
        -> writes auth.json

  OAUTH  (survives longer; needs your own Google Cloud client)
    1. Google Cloud Console -> Credentials -> Create OAuth client ID ->
       application type "TVs and Limited Input devices". Copy id + secret.
    2. Run:  python auth_setup.py oauth <client_id> <client_secret>
       Follow the device-code prompt in your browser.
    -> writes auth.json

Verify with:  python smoke_test.py
"""
import sys

OUT = "auth.json"


def main() -> int:
    method = sys.argv[1] if len(sys.argv) > 1 else "browser"

    if method == "browser":
        from ytmusicapi import setup
        print("Paste your YT Music request headers, then send EOF "
              "(Windows: Ctrl-Z + Enter; mac/Linux: Ctrl-D):")
        headers_raw = sys.stdin.read()
        if not headers_raw.strip():
            print("[FAIL] No headers received.")
            return 1
        setup(filepath=OUT, headers_raw=headers_raw)
        print(f"[ OK ] Wrote {OUT} (browser auth).")

    elif method == "oauth":
        if len(sys.argv) < 4:
            print("Usage: python auth_setup.py oauth <client_id> <client_secret>")
            return 1
        from ytmusicapi import setup_oauth
        client_id, client_secret = sys.argv[2], sys.argv[3]
        # Signature varies slightly across ytmusicapi versions; this matches >=1.4.
        setup_oauth(filepath=OUT, client_id=client_id,
                    client_secret=client_secret, open_browser=True)
        print(f"[ OK ] Wrote {OUT} (oauth).")

    else:
        print(f"Unknown method '{method}'. Use 'browser' or 'oauth'.")
        return 1

    print("Next:  python smoke_test.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
