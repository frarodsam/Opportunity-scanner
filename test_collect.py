"""Offline test: runs the whole pipeline against fake API responses."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collect  # noqa: E402

TODAY = datetime.now(timezone.utc).date()


def iso(days):
    return (TODAY + timedelta(days=days)).isoformat() + "T00:00:00+00:00"


def rw_item(i, title, type_name, closing_days):
    return {"id": i, "fields": {
        "title": title, "body": "Midterm evaluation of a school feeding program.",
        "how_to_apply": "Send proposals by email.", "url": "https://reliefweb.int/job/%d" % i,
        "date": {"created": iso(-2), "closing": iso(closing_days)},
        "source": [{"name": "CARE"}], "country": [{"name": "Malawi"}],
        "type": [{"name": type_name}], "career_categories": [{"name": "Monitoring and Evaluation"}],
    }}


def fake_http(url, body=None, timeout=60):
    if "reliefweb" in url:
        if body.get("query"):
            # title search returns one overlap (id 1) and one posted as a Job
            return {"data": [rw_item(1, "RFP: Midline Evaluation", "Consultancy", 30),
                             rw_item(3, "RFP Baseline Survey", "Job", 20)]}
        return {"data": [rw_item(1, "RFP: Midline Evaluation", "Consultancy", 30),
                         rw_item(2, "Closing soon consultancy", "Consultancy", 4),
                         rw_item(4, "Already closed", "Consultancy", -1)]}
    if "os=0" in url:
        return {"procnotices": [
            {"id": "OP1", "notice_type": "Request for Expression of Interest",
             "noticedate": TODAY.strftime("%d-%b-%Y"), "bid_description": "Baseline survey firm",
             "project_ctry_name": "Congo", "procurement_method_name": "Consultant Qualification Selection",
             "submission_deadline_date": iso(21), "notice_text": "<p>National health baseline</p>"},
            {"id": "OP2", "notice_type": "Request for Expression of Interest",
             "noticedate": TODAY.strftime("%d-%b-%Y"), "bid_description": "Procurement officer",
             "procurement_method_name": "Individual Consultant Selection",
             "bid_reference_no": "XX-CS-INDV", "submission_deadline_date": iso(15)},
            {"id": "OP3", "notice_type": "Contract Award", "noticedate": TODAY.strftime("%d-%b-%Y")},
            {"id": "OP4", "notice_type": "Request for Expression of Interest",
             "noticedate": (TODAY - timedelta(days=60)).strftime("%d-%b-%Y")},
        ]}
    return {"procnotices": []}


PAGE = """<html><a href="/about">About us</a>
<a href="/bids/rfp-endline-evaluation-togo.pdf">RFP: Endline evaluation, Togo school feeding</a>
<a href="https://example.org/bids/baseline-survey">Request for proposals: baseline survey in Benin</a>
<a href="#top">Back to the top of this page now</a></html>"""


def fake_text(url, timeout=60):
    return PAGE


class PipelineTest(unittest.TestCase):
    def run_once(self, tmp):
        with mock.patch.object(collect, "http_json", fake_http), \
             mock.patch.object(collect, "http_text", fake_text), \
             mock.patch.object(collect, "DATA", tmp), \
             mock.patch.object(collect, "WEEKLY", tmp / "weekly"), \
             mock.patch.object(collect, "SEEN_FILE", tmp / "seen.json"), \
             mock.patch.dict(os.environ, {"RELIEFWEB_APPNAME": "test"}):
            code = collect.main([])
        out = json.loads((tmp / "weekly" / (TODAY.isoformat() + ".json")).read_text())
        return code, out

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            code, out = self.run_once(tmp)
            self.assertEqual(code, 0)
            by_id = {p["id"]: p for p in out["postings"]}
            # dedupe within run, closed posting dropped, award and old notice dropped
            api_ids = {i for i in by_id if not i.startswith("page:")}
            self.assertEqual(api_ids, {"reliefweb:1", "reliefweb:2", "reliefweb:3",
                                       "worldbank:OP1", "worldbank:OP2"})
            # two watched pages share the fake HTML: 2 real links each, menu and anchor skipped
            page_titles = sorted(p["title"] for i, p in by_id.items() if i.startswith("page:"))
            self.assertEqual(len(page_titles), 3)  # one absolute link is shared by both pages
            self.assertTrue(all("About" not in t and "Back to" not in t for t in page_titles))
            self.assertIn("short runway", by_id["reliefweb:2"]["flags"])
            self.assertIn("staff vacancy", by_id["reliefweb:3"]["flags"])
            self.assertIn("roster", by_id["worldbank:OP2"]["flags"])
            self.assertEqual(by_id["worldbank:OP1"]["days_left"], 21)
            self.assertEqual(by_id["worldbank:OP1"]["text"], "National health baseline")
            self.assertIn("evaluation", by_id["reliefweb:1"]["keyword_hits"])
            # second run finds nothing new
            _, again = self.run_once(tmp)
            self.assertEqual(again["count"], 0)

    def test_missing_appname_skips_reliefweb(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with mock.patch.object(collect, "http_json", fake_http), \
                 mock.patch.object(collect, "http_text", fake_text), \
                 mock.patch.object(collect, "DATA", tmp), \
                 mock.patch.object(collect, "WEEKLY", tmp / "weekly"), \
                 mock.patch.object(collect, "SEEN_FILE", tmp / "seen.json"), \
                 mock.patch.dict(os.environ, {"RELIEFWEB_APPNAME": ""}):
                self.assertEqual(collect.main([]), 0)
            out = json.loads((tmp / "weekly" / (TODAY.isoformat() + ".json")).read_text())
            self.assertTrue(all(p["source"] != "ReliefWeb" for p in out["postings"]))


if __name__ == "__main__":
    unittest.main()
