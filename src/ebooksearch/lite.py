"""Server-rendered, no-JavaScript search page for limited browsers.

The main SPA relies on ES modules, ``fetch``, ``EventSource`` and modern JS
syntax that old e-reader WebKit engines (notably the Kindle Paperwhite's
"experimental browser") can't run. This page is plain HTML: a GET form and a
server-rendered result list with download links. No JS, no external CSS, no
long-lived connections — high contrast and large tap targets for e-ink.
"""
from __future__ import annotations

import html


def _fmt_bytes(n: int | None) -> str:
    if not n:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(n)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if (v >= 10 or i == 0) else f"{v:.1f} {units[i]}"


# Tuned for grayscale e-ink: pure black on white, big fonts, generous tap targets.
_STYLE = """
  body { font: 18px/1.5 sans-serif; color: #000; background: #fff; margin: 0; padding: 16px; }
  h1 { font-size: 22px; margin: 0 0 14px; }
  form { display: flex; gap: 8px; margin-bottom: 20px; }
  input[name=q] { flex: 1; font-size: 18px; padding: 10px; border: 2px solid #000; }
  button { font-size: 18px; padding: 10px 16px; border: 2px solid #000; background: #fff; color: #000; }
  .count { color: #444; margin-bottom: 12px; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { padding: 12px 0; border-bottom: 1px solid #ccc; }
  .title { font-weight: bold; }
  .meta { color: #444; font-size: 15px; margin: 2px 0 8px; }
  a.dl { display: inline-block; padding: 8px 14px; border: 2px solid #000; text-decoration: none; color: #000; }
"""


def render_lite_page(q: str, rows: list[dict], is_search: bool) -> str:
    q = q or ""
    items = []
    for b in rows:
        title = html.escape(b.get("title") or b.get("filename") or "Untitled")
        author = html.escape(b.get("author") or "")
        ext = html.escape((b.get("ext") or "").lstrip(".")).upper()
        size = _fmt_bytes(b.get("size_bytes"))
        meta = " · ".join(x for x in (author, ext, size) if x)
        items.append(
            f'<li><div class="title">{title}</div>'
            f'<div class="meta">{meta}</div>'
            f'<a class="dl" href="/api/download/{b["id"]}">Download</a></li>'
        )

    if is_search:
        n = len(rows)
        heading = f'{n} result{"" if n == 1 else "s"} for “{html.escape(q)}”'
    else:
        heading = "Recently indexed"

    body = "".join(items) if items else "<li>No matches.</li>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ebooksearch</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>ebooksearch</h1>
<form method="get" action="/lite">
<input name="q" type="text" value="{html.escape(q)}" placeholder="Search title, author, filename">
<button type="submit">Search</button>
</form>
<div class="count">{heading}</div>
<ul>{body}</ul>
</body>
</html>"""
