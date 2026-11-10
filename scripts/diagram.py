#!/usr/bin/env python3
"""Draw architecture.json as SVG. No dependencies, no network, deterministic output.

    python3 scripts/diagram.py                  # write docs/architecture-{light,dark}.svg
    python3 scripts/diagram.py --check          # exit 1 if what is on disk differs (CI)
    python3 scripts/diagram.py --svg            # print one SVG to stdout

The SVG is generated: edit architecture.json, never the output. Two themes because GitHub
serves README images into both light and dark pages, and one palette cannot serve both.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = ROOT / "architecture.json"
OUT = ROOT / "docs"

W = 1600
PAD = 32
LANE_LABEL_W = 232
LANE_R = 300                      # reserved for the join lane
BAND_H = 92
NODE_H = 56
NODE_GAP = 14
HEADER_H = 132
FOOTER_H = 56

LIGHT = dict(bg="#ffffff", band="#f6f7f9", band_edge="#e6e8ec", node="#ffffff",
             node_edge="#d3d7de", title="#10131a", sub="#5b6472", lane="#8a93a3",
             accent="#2f6fed", accent_soft="#dbe6fd", rule="#e9ebef")
DARK = dict(bg="#0d1117", band="#151b23", band_edge="#232b36", node="#161d26",
            node_edge="#3a4653", title="#e9eef6", sub="#a3aebc", lane="#7f8a97",
            accent="#5b9bff", accent_soft="#1b2b45", rule="#1e2530")


def text_w(s: str, size: float) -> float:
    """Cheap text metric, deliberately pessimistic.

    SVG cannot measure text, and the renderer's font is not ours to choose: a Linux box falls
    back to a wide grotesque while a Mac renders SF, so a width that fits in one clips in the
    other. The factors here are calibrated against the wide fallback — if it fits here it fits
    on the narrower fonts too.
    """
    wide = sum(1 for c in s if c in "mwMW@%")
    narrow = sum(1 for c in s if c in "iltfjI.,:;'|!")
    return size * (0.62 * len(s) + 0.14 * wide - 0.10 * narrow)


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def layout(spec: dict) -> tuple[list[dict], dict, int]:
    lanes = {l["id"]: l for l in spec["lanes"]}
    order = [l["id"] for l in spec["lanes"]]
    by_lane: dict[str, list] = {k: [] for k in order}
    for n in spec["nodes"]:
        by_lane.setdefault(n["lane"], []).append(dict(n))

    x0 = PAD + LANE_LABEL_W
    x_max = W - PAD - LANE_R
    y = HEADER_H
    placed: dict[str, dict] = {}
    rows = []

    for lid in order:
        band_top = y
        cx, cy = x0, band_top + 22
        for n in by_lane[lid]:
            w = max(172.0, min(382.0,
                               text_w(n["title"], 13.5) + 54,
                               text_w(n.get("sub", ""), 11.5) + 50))
            if cx + w > x_max and cx > x0:
                cx = x0
                cy += NODE_H + NODE_GAP
            placed[n["id"]] = dict(n, x=cx, y=cy, w=w, h=NODE_H,
                                   cx=cx + w / 2, cy=cy + NODE_H / 2)
            cx += w + NODE_GAP
        band_h = (cy - band_top) + NODE_H + 26
        rows.append(dict(id=lid, lane=lanes[lid], top=band_top, h=band_h))
        y = band_top + band_h + 12

    return rows, placed, y + FOOTER_H


def render(spec: dict, pal: dict) -> str:
    rows, nodes, height = layout(spec)
    out: list[str] = []
    a = out.append

    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" '
      f'viewBox="0 0 {W} {height}" role="img" aria-label="{esc(spec["title"])}">')
    a('<style>'
      'text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}'
      '.t{font-size:13.5px;font-weight:600}'
      '.s{font-size:11.5px}'
      '.l{font-size:13px;font-weight:650;letter-spacing:.02em}'
      '.b{font-size:11px}'
      '.n{font-size:19px;font-weight:650}'
      '.jl{font-size:10.5px;font-weight:500}'
      '</style>')
    a(f'<rect width="{W}" height="{height}" fill="{pal["bg"]}"/>')

    # header
    a(f'<text class="n" x="{PAD}" y="56" fill="{pal["title"]}">{esc(spec["title"])}</text>')
    a(f'<text class="s" x="{PAD}" y="82" fill="{pal["sub"]}">{esc(spec["subtitle"])}</text>')
    checks = spec.get("checks")
    if checks:
        a(f'<text class="b" x="{W - PAD}" y="56" text-anchor="end" '
          f'fill="{pal["sub"]}">{esc(checks)}</text>')
    a(f'<text class="b" x="{W - PAD}" y="80" text-anchor="end" fill="{pal["lane"]}">'
      f'generated from architecture.json</text>')
    a(f'<line x1="{PAD}" y1="98" x2="{W - PAD}" y2="98" stroke="{pal["rule"]}"/>')

    # lanes
    joined = {j["from"] for j in spec["joins"]} | {j["to"] for j in spec["joins"]}
    for r in rows:
        lane = r["lane"]
        band_x = PAD + 8
        a(f'<rect x="{band_x}" y="{r["top"]}" width="{W - 2 * PAD - 16}" height="{r["h"]}" '
          f'rx="14" fill="{pal["band"]}" stroke="{pal["band_edge"]}"/>')
        a(f'<text class="l" x="{PAD + 28}" y="{r["top"] + 30}" fill="{pal["lane"]}">'
          f'{esc(lane["label"])}</text>')
        a(f'<text class="b" x="{PAD + 28}" y="{r["top"] + 48}" fill="{pal["sub"]}">'
          f'{esc(lane["blurb"])}</text>')

        for n in nodes.values():
            if n["lane"] != r["id"]:
                continue
            x, y_, w, h = n["x"], n["y"], n["w"], n["h"]
            edge = pal["accent"] if n["id"] in joined else pal["node_edge"]
            a(f'<rect x="{x}" y="{y_}" width="{w}" height="{h}" rx="11" '
              f'fill="{pal["node"]}" stroke="{edge}" stroke-width="1.25"/>')
            a(f'<circle cx="{x + 15}" cy="{y_ + h / 2}" r="3" fill="{pal["accent"]}"/>')
            a(f'<text class="t" x="{x + 27}" y="{y_ + 24}" fill="{pal["title"]}">'
              f'{esc(n["title"])}</text>')
            if n.get("sub"):
                a(f'<text class="s" x="{x + 27}" y="{y_ + 41}" fill="{pal["sub"]}">'
                  f'{esc(n["sub"])}</text>')

    # The four joins, as a panel in the right lane. Curved arrows across bands read as
    # spaghetti the moment a joined node is not the rightmost in its row, so the topology is
    # written out instead and the joined nodes carry the accent outline.
    panel_x = W - PAD - LANE_R + 22
    panel_w = LANE_R - 46
    rows_h = 46
    panel_h = 58 + rows_h * len(spec["joins"])
    a(f'<rect x="{panel_x}" y="{HEADER_H - 10}" width="{panel_w}" height="{panel_h}" rx="14" '
      f'fill="{pal["band"]}" stroke="{pal["band_edge"]}"/>')
    a(f'<text class="l" x="{panel_x + 18}" y="{HEADER_H + 18}" fill="{pal["lane"]}">'
      f'The four joins</text>')
    for i, j in enumerate(spec["joins"]):
        src, dst = nodes.get(j["from"]), nodes.get(j["to"])
        ry = HEADER_H - 10 + 44 + i * rows_h
        a(f'<circle cx="{panel_x + 22}" cy="{ry + 4}" r="3.2" fill="{pal["accent"]}"/>')
        label = "%s → %s" % (src["title"] if src else j["from"], dst["title"] if dst else j["to"])
        a(f'<text class="jl" x="{panel_x + 34}" y="{ry + 8}" fill="{pal["title"]}">'
          f'{esc(label)}</text>')
        a(f'<text class="b" x="{panel_x + 34}" y="{ry + 24}" fill="{pal["sub"]}">'
          f'{esc(j["label"])}</text>')

    a(f'<text class="b" x="{PAD}" y="{height - 22}" fill="{pal["lane"]}">'
      f'structure only · whether a machine matches is what sync/align.py answers</text>')
    a('</svg>')
    return "\n".join(out) + "\n"


def validate(spec: dict) -> list[str]:
    """Structural faults that would silently ruin the drawing."""
    problems: list[str] = []
    lanes = {l["id"] for l in spec["lanes"]}
    ids = [n["id"] for n in spec["nodes"]]
    seen = set()
    for n in spec["nodes"]:
        if n["id"] in seen:
            problems.append("duplicate node id %r" % n["id"])
        seen.add(n["id"])
        if n["lane"] not in lanes:
            problems.append("node %r names unknown lane %r" % (n["id"], n["lane"]))
        for key in ("title", "lane"):
            if not n.get(key):
                problems.append("node %r is missing %s" % (n["id"], key))
    for j in spec.get("joins", []):
        for side in ("from", "to"):
            if j.get(side) not in seen:
                problems.append("join names unknown node %r" % j.get(side))
    if not spec.get("lanes"):
        problems.append("no lanes")
    if not spec.get("nodes"):
        problems.append("no nodes")
    return problems


def main(argv):
    p = argparse.ArgumentParser(description="Draw the architecture as SVG.")
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--check", action="store_true",
                   help="exit 1 if the SVG on disk differs from what this would generate")
    p.add_argument("--validate", action="store_true",
                   help="exit 1 if architecture.json is structurally wrong")
    p.add_argument("--svg", action="store_true", help="print the light SVG to stdout")
    a = p.parse_args(argv)

    spec = json.loads(SPEC.read_text())
    problems = validate(spec)
    if problems:
        for prob in problems:
            print("invalid: %s" % prob)
        return 1
    if a.validate:
        print("architecture.json: ok (%d lanes, %d nodes, %d joins)"
              % (len(spec["lanes"]), len(spec["nodes"]), len(spec.get("joins", []))))
        return 0
    checks = spec.get("checks")
    profile = ROOT / "sync" / "profile.json"
    if not checks and profile.exists():
        try:
            n = len(json.loads(profile.read_text())["items"])
            spec["checks"] = "%d checks · sync/align.py" % n
        except (KeyError, ValueError):
            pass

    svgs = {"architecture-light.svg": render(spec, LIGHT),
            "architecture-dark.svg": render(spec, DARK)}

    if a.svg:
        sys.stdout.write(svgs["architecture-light.svg"])
        return 0

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.check:
        stale = [n for n, body in svgs.items()
                 if not (out / n).exists() or (out / n).read_text() != body]
        for n in stale:
            print("stale: %s" % (out / n))
        print("diagram %s" % ("stale" if stale else "up to date"))
        return 1 if stale else 0

    for name, body in svgs.items():
        (out / name).write_text(body)
        print("wrote %s (%d bytes)" % (out / name, len(body)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
