#!/usr/bin/env python3
"""Read Overture Maps GeoParquet straight out of S3 over HTTP range requests.

Overture ships ~450 MB parquet shards. We want a few hundred metres of one city,
so downloading a shard to read 0.001% of it is absurd. GeoParquet stores a bbox
struct per row and per-row-group statistics over it, which means the footer alone
tells us which row groups can possibly intersect our area — usually one or two
out of hundreds.

The AWS SDK does not reliably honour this environment's HTTPS proxy, so this
implements the minimum needed instead: a seekable file object backed by HTTP
Range headers through urllib. pyarrow accepts any object with read/seek/tell.
"""

import io
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

S3 = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"


class HttpRangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range, with a small read-ahead cache."""

    def __init__(self, url, block=1 << 20):
        self.url = url
        self.pos = 0
        self.block = block
        self._cache = {}
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            self.size = int(r.headers["Content-Length"])

    def _fetch(self, start, end):
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        out = bytearray()
        pos = self.pos
        while len(out) < n:
            bi = pos // self.block
            if bi not in self._cache:
                s = bi * self.block
                e = min(s + self.block, self.size) - 1
                self._cache[bi] = self._fetch(s, e)
                if len(self._cache) > 64:                     # bounded cache
                    self._cache.pop(next(iter(self._cache)))
            blk = self._cache[bi]
            off = pos - bi * self.block
            take = min(len(blk) - off, n - len(out))
            out += blk[off:off + take]
            pos += take
        self.pos = pos
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def list_shards(release, theme, typ):
    keys, token = [], None
    prefix = f"release/{release}/theme%3D{theme}/type%3D{typ}/"
    while True:
        url = f"{S3}/?list-type=2&prefix={prefix}&max-keys=1000"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token, safe="")
        xml = urllib.request.urlopen(url, timeout=90).read().decode()
        keys += [k for k in re.findall(r"<Key>([^<]+)</Key>", xml) if k.endswith(".parquet")]
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        if not m:
            break
        token = m.group(1)
    return keys


def _intersecting_groups(key, bbox):
    """Return (key, [row group indices]) whose bbox statistics hit the box."""
    import pyarrow.parquet as pq
    xmin, ymin, xmax, ymax = bbox
    try:
        md = pq.ParquetFile(HttpRangeFile(f"{S3}/{key}")).metadata
    except Exception:
        return key, []
    rg0 = md.row_group(0)
    names = [rg0.column(i).path_in_schema for i in range(md.num_columns)]
    try:
        ix = {n: names.index(n) for n in
              ("bbox.xmin", "bbox.xmax", "bbox.ymin", "bbox.ymax")}
    except ValueError:
        return key, []
    want = []
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        gx0 = rg.column(ix["bbox.xmin"]).statistics.min
        gx1 = rg.column(ix["bbox.xmax"]).statistics.max
        gy0 = rg.column(ix["bbox.ymin"]).statistics.min
        gy1 = rg.column(ix["bbox.ymax"]).statistics.max
        if gx1 >= xmin and gx0 <= xmax and gy1 >= ymin and gy0 <= ymax:
            want.append(g)
    return key, want


def read_bbox(release, theme, typ, bbox, columns=None, progress=print, workers=16):
    """Yield pyarrow Tables of rows whose bbox intersects (xmin, ymin, xmax, ymax).

    Footers are scanned concurrently: reading 128 footers serially costs minutes,
    and the whole point is to touch as little of the data as possible.
    """
    import pyarrow.parquet as pq
    shards = list_shards(release, theme, typ)
    progress(f"{theme}/{typ}: scanning {len(shards)} shard footers")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        found = [(k, g) for k, g in pool.map(lambda k: _intersecting_groups(k, bbox), shards) if g]
    progress(f"  {len(found)} shard(s) intersect: " +
             ", ".join(f"{k.split('-')[1]}:{len(g)}rg" for k, g in found[:6]))
    hits = 0
    for key, groups in found:
        t = pq.ParquetFile(HttpRangeFile(f"{S3}/{key}")).read_row_groups(groups, columns=columns)
        hits += t.num_rows
        yield t
    progress(f"  {hits:,} candidate rows read")
