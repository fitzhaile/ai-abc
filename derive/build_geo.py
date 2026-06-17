#!/usr/bin/env python3
"""Extract simplified US state polygons + population into a small committed JSON
for the dashboard's state choropleths (clubs-by-state and clubs-per-capita).

Sources (public domain, auto-downloaded into data/geo_raw/ if missing — that
dir is gitignored and re-fetchable; the *derived* output here is committed):

  - State boundaries: US Census cartographic boundaries cb_2020_us_state_5m
    (NAD83 lat/lng, 1:5,000,000). Geometry kept in lng/lat; the dashboard
    projects it with Albers at render time. The 5m file (vs 20m) keeps the
    coastline accurate enough that coastal clubs plot inside their state, and
    includes the US Virgin Islands as a real polygon.
    https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_state_5m.zip

  - Population: US Census Population Estimates Program, vintage 2023, total
    population POPESTIMATE2023 (key-free CSV).
    https://www2.census.gov/programs-surveys/popest/datasets/2020-2023/state/totals/NST-EST2023-ALLDATA.csv

Output (committed): data/extracted/state_geo.json

This is stdlib-only: it parses the ESRI .shp/.dbf directly and simplifies with
Douglas-Peucker, so the project takes on no GDAL/geo dependency.
"""
import csv, json, os, struct, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GEO_RAW = os.path.join(ROOT, "data", "geo_raw")
OUT = os.path.join(ROOT, "data", "extracted", "state_geo.json")

STATES_ZIP_URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_state_5m.zip"
POP_URL = ("https://www2.census.gov/programs-surveys/popest/datasets/"
           "2020-2023/state/totals/NST-EST2023-ALLDATA.csv")
POP_FIELD = "POPESTIMATE2023"

# The 5m file carries far-Pacific territories with no clubs (Guam, American
# Samoa, N. Mariana Is., minor outlying islands). Drop them — they'd need their
# own off-screen insets and add nothing. Keep the 50 states + DC + PR + USVI.
EXCLUDE_ABBR = {"GU", "AS", "MP", "UM"}

SIMPLIFY_EPS = 0.02   # degrees (~0.3px on the rendered CONUS panel — keeps the
                      # coastline honest so coastal clubs fall inside the state)
ROUND_DP = 3          # ~100 m


# ---------- ESRI shapefile readers (stdlib) ----------
def read_dbf(path):
    with open(path, "rb") as f:
        data = f.read()
    n = struct.unpack("<I", data[4:8])[0]
    header_len = struct.unpack("<H", data[8:10])[0]
    rec_len = struct.unpack("<H", data[10:12])[0]
    fields, off = [], 32
    while data[off] != 0x0D:
        fields.append((data[off:off + 11].split(b"\x00")[0].decode("latin-1"), data[off + 16]))
        off += 32
    rows = []
    for i in range(n):
        rec = data[header_len + i * rec_len: header_len + (i + 1) * rec_len]
        vals, p = [], 1
        for _, length in fields:
            vals.append(rec[p:p + length].decode("latin-1").strip())
            p += length
        rows.append(vals)
    return [f[0] for f in fields], rows


def read_shp_polygons(path):
    with open(path, "rb") as f:
        data = f.read()
    out, pos, end = [], 100, len(data)
    while pos < end:
        content_words = struct.unpack(">i", data[pos + 4:pos + 8])[0]
        start = pos + 8
        if struct.unpack("<i", data[start:start + 4])[0] != 5:
            out.append([]); pos = start + content_words * 2; continue
        o = start + 4 + 32
        n_parts, n_points = struct.unpack("<ii", data[o:o + 8]); o += 8
        parts = list(struct.unpack("<%di" % n_parts, data[o:o + 4 * n_parts])); o += 4 * n_parts
        coords = struct.unpack("<%dd" % (n_points * 2), data[o:o + 16 * n_points])
        bounds = parts + [n_points]
        rings = [[[coords[i * 2], coords[i * 2 + 1]] for i in range(bounds[k], bounds[k + 1])]
                 for k in range(n_parts)]
        out.append(rings)
        pos = start + content_words * 2
    return out


# ---------- Douglas-Peucker simplification (iterative, stdlib) ----------
def _perp(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def simplify_ring(pts, eps):
    if len(pts) < 3:
        return pts[:]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        s, e = stack.pop()
        dmax, idx = 0.0, -1
        for i in range(s + 1, e):
            d = _perp(pts[i], pts[s], pts[e])
            if d > dmax:
                dmax, idx = d, i
        if idx != -1 and dmax > eps:
            keep[idx] = True
            stack.append((s, idx)); stack.append((idx, e))
    out = []
    for i, k in enumerate(keep):
        if k:
            pt = [round(pts[i][0], ROUND_DP), round(pts[i][1], ROUND_DP)]
            if not out or out[-1] != pt:
                out.append(pt)
    return out


def ensure_raw():
    os.makedirs(GEO_RAW, exist_ok=True)
    sdir = os.path.join(GEO_RAW, "states")
    if not (os.path.isdir(sdir) and any(n.endswith(".shp") for n in os.listdir(sdir))):
        print("Downloading state boundaries…")
        zp = os.path.join(GEO_RAW, "states.zip")
        urllib.request.urlretrieve(STATES_ZIP_URL, zp)
        with zipfile.ZipFile(zp) as zf:
            zf.extractall(sdir)
    pop = os.path.join(GEO_RAW, "state_pop.csv")
    if not os.path.exists(pop):
        print("Downloading population estimates…")
        urllib.request.urlretrieve(POP_URL, pop)
    return sdir, pop


def main():
    sdir, pop_csv = ensure_raw()
    shp = [os.path.join(sdir, n) for n in os.listdir(sdir) if n.endswith(".shp")][0]
    dbf = shp[:-4] + ".dbf"
    names, rows = read_dbf(dbf)
    shapes = read_shp_polygons(shp)
    iF, iA, iN = names.index("STATEFP"), names.index("STUSPS"), names.index("NAME")

    # population by zero-padded FIPS, state rows only (SUMLEV 040)
    pop = {}
    with open(pop_csv, newline="", encoding="latin-1") as f:
        for r in csv.DictReader(f):
            if r.get("SUMLEV") == "040":
                pop[r["STATE"].zfill(2)] = int(r[POP_FIELD])

    states, kept_pts, raw_pts = {}, 0, 0
    for i, row in enumerate(rows):
        fips, abbr, name = row[iF].zfill(2), row[iA], row[iN]
        if abbr in EXCLUDE_ABBR:
            continue
        rings = []
        for ring in shapes[i]:
            raw_pts += len(ring)
            s = simplify_ring(ring, SIMPLIFY_EPS)
            if len(s) >= 4:
                rings.append(s); kept_pts += len(s)
        states[abbr] = {"name": name, "fips": fips,
                        "population": pop.get(fips), "rings": rings}

    out = {
        "_provenance": {
            "geometry": ("US Census cartographic boundaries cb_2020_us_state_20m "
                         "(NAD83 lng/lat, 1:20m), Douglas-Peucker simplified "
                         "eps=%g deg, coords rounded to %d dp" % (SIMPLIFY_EPS, ROUND_DP)),
            "geometry_url": STATES_ZIP_URL,
            "population": "US Census Population Estimates Program, %s (vintage 2023)" % POP_FIELD,
            "population_url": POP_URL,
            "note": "rings are [lng,lat]; the dashboard projects them with Albers at render time",
        },
        "states": states,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")
    size = os.path.getsize(OUT)
    print("Wrote %s — %d states, points %d→%d (%.0f%%), %.1f KB" %
          (os.path.relpath(OUT, ROOT), len(states), raw_pts, kept_pts,
           100.0 * kept_pts / raw_pts, size / 1024.0))
    missing = [a for a, s in states.items() if s["population"] is None]
    if missing:
        print("  no population for:", ", ".join(sorted(missing)), "(per-capita will be n/a there)")


if __name__ == "__main__":
    main()
