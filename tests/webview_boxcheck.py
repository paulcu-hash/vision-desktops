"""WebView2 box check: opens a real WebView2 window (pywebview uses the
Edge WebView2 runtime) with an identifying title and keeps it open.
Launched via app_launch (ownership -> job/tree -> auto-move into the box).
"""
import sys

import webview

HTML = """<html><body style="background:#102030;color:#fff;font-family:sans-serif">
<h1>WEBVIEW-BOXCHECK</h1><p>Edge WebView2 is running in the box.</p></body></html>"""

if __name__ == "__main__":
    webview.create_window("WEBVIEW-BOXCHECK", html=HTML, width=640, height=400)
    webview.start()
    sys.exit(0)
