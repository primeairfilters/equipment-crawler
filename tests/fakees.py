"""A fake Socrata server for the ENERGY STAR dataset 83eb-xbyy, plus a row builder.

Column names come from crawler.energystar.FIELDS (logical name -> real column name), so these
tests do not guess what the real dataset calls its columns. UNVERIFIED: that FIELDS holds the
real names is the builder's to confirm against the live dataset.
"""
import json
import uuid
from urllib.parse import parse_qs, urlsplit

DATASET = "83eb-xbyy"
JSON = {"Content-Type": "application/json"}


class FakeSocrata:
    def __init__(self, site, rows=None, updated_at=1790000000):
        self.site = site
        self.rows = list(rows or [])
        self.updated_at = updated_at
        self.fail_offsets = set()
        self.fail_meta = False
        site.route(f"/api/views/{DATASET}.json", self._meta)
        site.route(f"/resource/{DATASET}.json", self._data)

    def _meta(self, rec):
        if self.fail_meta:
            return 500, {}, b"down"
        return 200, JSON, json.dumps({"id": DATASET, "rowsUpdatedAt": self.updated_at}).encode()

    def _data(self, rec):
        query = parse_qs(urlsplit(rec["path"]).query)
        limit = int(query["$limit"][0])
        offset = int(query.get("$offset", ["0"])[0])
        if offset in self.fail_offsets:
            return 500, {}, b"boom"
        return 200, JSON, json.dumps(self.rows[offset:offset + limit]).encode()

    def data_requests(self):
        return [r for r in self.site.requests if r["path"].startswith("/resource/")]

    def meta_requests(self):
        return [r for r in self.site.requests if r["path"].startswith("/api/views/")]

    def offsets(self):
        return [int(parse_qs(urlsplit(r["path"]).query).get("$offset", ["0"])[0]) for r in self.data_requests()]


def es_row(brand, ahri, outdoor, indoor, furnace="", *, seer2="16.5", eer2="12.1", hspf2="", cooling="24000", heating="",
           row_id=None, indoor_brand=None):
    """One dataset row, keyed by the real column names. Blank strings stand for 'not given'; None leaves the column out."""
    from crawler.energystar import FIELDS
    values = {"row_id": row_id or uuid.uuid4().hex[:12], "brand": brand, "ahri_ref": ahri, "outdoor_model": outdoor,
              "indoor_brand": indoor_brand,
              "indoor_model": indoor, "furnace_model": furnace, "seer2": seer2, "eer2": eer2, "hspf2": hspf2,
              "cooling_btuh": cooling, "heating_btuh": heating}
    return {FIELDS[k]: v for k, v in values.items() if v is not None}


def ahri():
    return "AH" + uuid.uuid4().hex[:10].upper()
