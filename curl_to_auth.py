#!/usr/bin/env python3
"""Fallback auth: build auth.json from a Chrome 'Copy as cURL (bash)' command.

Use this when the DevTools 'Raw' request-headers toggle isn't showing.

  1. Network tab -> right-click the /browse request -> Copy -> "Copy as cURL (bash)".
  2. Run:  python curl_to_auth.py
     Paste the cURL command, then EOF (Windows: Ctrl-Z + Enter; mac/Linux: Ctrl-D).
  -> writes auth.json

It extracts the request headers (including cookie) from the cURL and hands them to
ytmusicapi. Nothing is sent anywhere - it's all local.
"""
import shlex
import sys

OUT = "auth.json"


def curl_to_headers_raw(curl: str) -> str:
    # Flatten bash / cmd line-continuations.
    curl = curl.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")
    # shlex can't handle bash ANSI-C $'...' quoting; drop the $ so the quotes parse.
    curl = curl.replace("$'", "'")
    try:
        tokens = shlex.split(curl, posix=True)
    except ValueError:
        tokens = curl.split()

    headers = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header") and i + 1 < len(tokens):
            kv = tokens[i + 1]
            if ":" in kv:
                k, v = kv.split(":", 1)
                headers[k.strip().lower()] = v.strip()
            i += 2
        elif t in ("-b", "--cookie") and i + 1 < len(tokens):
            headers["cookie"] = tokens[i + 1]
            i += 2
        else:
            i += 1

    if "cookie" not in headers:
        raise SystemExit(
            "[FAIL] No cookie found in the cURL. Make sure you used "
            "'Copy as cURL (bash)' on a logged-in /browse request."
        )
    return "\n".join(f"{k}: {v}" for k, v in headers.items())


def main() -> int:
    try:
        from ytmusicapi import setup
    except ImportError:
        print("[FAIL] ytmusicapi not installed. Run: "
              r".\.venv\Scripts\python.exe -m pip install -r requirements.txt")
        return 1

    print("Paste the 'Copy as cURL (bash)' command, then send EOF "
          "(Windows: Ctrl-Z + Enter; mac/Linux: Ctrl-D):")
    curl = sys.stdin.read()
    if "curl" not in curl:
        print("[FAIL] That doesn't look like a cURL command.")
        return 1

    headers_raw = curl_to_headers_raw(curl)
    setup(filepath=OUT, headers_raw=headers_raw)
    print(f"[ OK ] Wrote {OUT}. Next: python smoke_test.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
