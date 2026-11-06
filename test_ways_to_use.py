#!/usr/bin/env python3
"""Guard for index.html's "Ways to use QTPD" (#ways-to-use, SEO handover 2026-11-06): the panel
of the ? help menu in the header, a native <details> whose summary is the ? button.

The section's headings link to filtered views of the list, which makes it a surface that puts
games in front of a visitor who did not ask for anything specific. House rule (CLAUDE.md, Product
rules): every such surface sets `adult=hide`, and never carries a `q=` search, which would lift it.
presets.py enforces this for the shelves; this does the same for these links. pages.yml runs it
before every deploy and refuses to publish when it fails.

It also holds the section to the rules that keep it SEO and not spam: static HTML, folded only by
a native <details> that starts closed, one full copy (how-it-works.html links here, it does not
repeat the groups), and link parameters the page's own URL parser actually reads.

Reads the two HTML files only. No network, no data files.  Usage: python3 test_ways_to_use.py
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
INDEX = (HERE / "index.html").read_text(encoding="utf-8")
HOW = (HERE / "how-it-works.html").read_text(encoding="utf-8")

fails = []

def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails.append(name)


class Section(HTMLParser):
    """Collects the #ways-to-use <details>: its attributes, headings, links and text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0            # nesting inside the <details>, 0 = outside
        self.attrs = None
        self.tag_stack = []
        self.h2, self.h3, self.links, self.text = [], [], [], []
        self.hidden = []          # any element inside with a hidden attribute or inline display:none
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.depth == 0:
            if tag == "details" and a.get("id") == "ways-to-use":
                self.attrs, self.depth = a, 1
            return
        if tag == "details":
            self.depth += 1
        if tag in ("script", "noscript", "template"):
            self.in_script = True
        if "hidden" in a or re.search(r"display\s*:\s*none", a.get("style") or ""):
            self.hidden.append(tag)
        if tag == "a":
            self.links.append(a.get("href", ""))
        self.tag_stack.append(tag)

    def handle_endtag(self, tag):
        if self.depth == 0:
            return
        if tag == "details":
            self.depth -= 1
            return
        if tag in ("script", "noscript", "template"):
            self.in_script = False
        if self.tag_stack:
            self.tag_stack.pop()

    def handle_data(self, data):
        if self.depth == 0 or not data.strip():
            return
        if "h2" in self.tag_stack:
            self.h2.append(data.strip())
        if "h3" in self.tag_stack:
            self.h3.append(data.strip())
        self.text.append(data.strip())


p = Section()
p.feed(INDEX)

print("\n== the section exists and is folded natively ==")
check("exactly one #ways-to-use in index.html", INDEX.count('id="ways-to-use"') == 1)
check("it is a <details>", p.attrs is not None)
check("closed by default (no `open` attribute)", p.attrs is not None and "open" not in p.attrs)
check("nothing inside is hidden by [hidden] or inline display:none", not p.hidden)
check("no <script>/<noscript>/<template> inside: the text is plain static HTML", "<script" not in
      INDEX[INDEX.find('id="ways-to-use"'):INDEX.find("</details>", INDEX.find('id="ways-to-use"'))])


class Wrappers(HTMLParser):
    """Which hiding elements (<noscript>, <template>) are open where #ways-to-use starts."""
    def __init__(self):
        super().__init__()
        self.open, self.at = [], None
    def handle_starttag(self, tag, attrs):
        if tag in ("noscript", "template"):
            self.open.append(tag)
        if dict(attrs).get("id") == "ways-to-use":
            self.at = list(self.open)
    def handle_endtag(self, tag):
        if tag in ("noscript", "template") and self.open and self.open[-1] == tag:
            self.open.pop()


w = Wrappers()
w.feed(INDEX)
check("not inside a <noscript> or <template>: browsers and crawlers both get it",
      w.at == [])
check("section heading (h2) is 'Ways to use QTPD'", "".join(p.h2) == "Ways to use QTPD")
check("the summary is the ? help button", '<summary class="util-btn help-btn" id="helpBtn"' in INDEX)

print("\n== ten groups, each a heading plus one sentence ==")
heads = [h for h in p.h3]
check(f"ten h3 headings (found {len(heads)})", len(heads) == 10)
check("enough real text to be worth indexing (> 1,200 characters)", len(" ".join(p.text)) > 1200)

print("\n== every games link: adult=hide, no q=, parameters the page reads ==")
# The keys loadFromURL() reads, taken from the page itself so this cannot drift from it.
lfu = INDEX[INDEX.find("function loadFromURL()"):INDEX.find("function applyURLRange()")]
known = set(re.findall(r'setIf\("([a-z]+)"', lfu)) | set(re.findall(r'readTri\("([a-z]+)"', lfu))
tok = re.search(r"const TOK = \{([^}]*)\}", lfu)
flag_tokens = set(re.findall(r"(\w+):", tok.group(1))) if tok else set()
check("found the page's URL keys and flag tokens", len(known) > 20 and {"kac", "iap", "drm"} <= flag_tokens)

views = [h for h in p.links if h.startswith("?")]
check(f"the section links to filtered views (found {len(views)})", len(views) >= 9)
for href in views:
    q = parse_qs(href[1:], keep_blank_values=True)
    label = href[:60] + ("…" if len(href) > 60 else "")
    check(f"adult=hide        {label}", q.get("adult") == ["hide"])
    check(f"no q= search      {label}", "q" not in q)
    unknown = set(q) - known
    check(f"keys all read     {label}" + (f"  (unknown: {sorted(unknown)})" if unknown else ""), not unknown)
    for key in ("flags", "noflags"):
        for t in (q.get(key, [""])[0].split(",") if key in q else []):
            check(f"{key} token '{t}' exists", t in flag_tokens)
others = [h for h in p.links if not h.startswith("?")]
check("the other links are How it works and the wishlist jump (#wishBox, a real id)",
      sorted(others) == ["#wishBox", "how-it-works.html"] and INDEX.count('id="wishBox"') == 1)

print("\n== one full copy: how-it-works.html points here and does not repeat it ==")
check("how-it-works.html links to ./#ways-to-use", 'href="./#ways-to-use"' in HOW)
repeated = [h for h in heads if h in HOW]
check("no group heading is copied into how-it-works.html" + (f" (found {repeated})" if repeated else ""),
      not repeated)
check("how-it-works.html has no canonical pointing at the tool page",
      'rel="canonical" href="https://mlmariss.github.io/SteamQTPD/"' not in HOW)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED"))
sys.exit(1 if fails else 0)
