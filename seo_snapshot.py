#!/usr/bin/env python3
"""seo_snapshot.py — write a plain-HTML "top picks" snapshot into the DEPLOYED index.html.

Why: the game list exists only after JavaScript has loaded the data layers. Google renders
JavaScript, but Bing and the AI search crawlers (ChatGPT, Perplexity, Claude) mostly do not, so
to them the page was a header, a footer and a list of filter labels. This puts real content in
the HTML: each preset shelf's label, blurb, a link to it, and its current top titles.

Source is presets.json, NOT a fresh computation. presets.py already ranks every shelf with the
page's own filter logic, and fails its job if a shelf omits `adult=hide` (house rule: never
highlight adult content on a surface nobody asked for — this snapshot is one). Re-deriving QTPD
here would be a second copy of the maths to drift. As a second lock, a shelf whose query lacks
`adult=hide` is skipped here too.

Runs in pages.yml against _site/index.html only, so the repo's index.html never churns with
data. It fills the space between the SEO_SNAPSHOT markers; the page's first render() removes
the section, so a visitor sees the live list, not this.

Fails soft: no presets.json, or a broken one, leaves the markers empty and exits 0. A missing
snapshot is a smaller loss than a site that stopped deploying.

Usage: python3 seo_snapshot.py _site/index.html [presets.json]
"""

import html, json, sys, time

START, END = "<!--SEO_SNAPSHOT_START-->", "<!--SEO_SNAPSHOT_END-->"


def build(p):
    esc = html.escape
    shelves = []
    for s in p.get("presets", []):
        q = s.get("query", "")
        if "adult=hide" not in q.split("&") or not s.get("sample"):
            continue
        items = "".join(f"<li>{esc(t)}</li>" for t in s["sample"])
        shelves.append(
            f'<div class="snap-shelf"><h3><a href="?{esc(q)}">{esc(s["label"])}</a></h3>'
            f'<p>{esc(s.get("blurb", ""))}</p><ol>{items}</ol></div>')
    if not shelves:
        return ""
    day = time.strftime("%Y-%m-%d", time.gmtime(p.get("generated_at", time.time())))
    return (f'<section class="seo-snap" id="seoSnapshot" aria-label="Top picks">'
            f'<h2>Top picks right now <span>· updated {day}</span></h2>'
            f'<div class="snap-grid">{"".join(shelves)}</div></section>')


def main():
    page = sys.argv[1]
    src = sys.argv[2] if len(sys.argv) > 2 else "presets.json"
    s = open(page, encoding="utf-8").read()
    if s.count(START) != 1 or s.count(END) != 1:
        sys.exit(f"{page}: expected exactly one {START} … {END} pair")
    try:
        snap = build(json.load(open(src, encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"::warning::seo_snapshot: {src} unusable ({e}); deploying without the snapshot")
        snap = ""
    a, rest = s.split(START)
    _, b = rest.split(END)
    open(page, "w", encoding="utf-8").write(a + START + snap + END + b)
    print(f"seo_snapshot: wrote {snap.count('<li>')} titles in {snap.count('snap-shelf')} shelves")


if __name__ == "__main__":
    main()
