"""Every registered GET and person decision, for every bound viewer."""
import copy
import json
import urllib.parse
import unittest
from unittest.mock import patch

from decide_kit import fixture_rows
from test_api_routes import ApiCase


class MemberPrivacyTests(ApiCase):
    def test_registered_views_and_decisions(self):
        s = self.s
        self.tokens["guest"] = s.new_client("privacy-guest", ["read", "decide"], "guest")
        m = s.get_model(False)
        metas = [{"id": key, "type": "movie", "name": title, "description": "AnaTitleSentinel"}
                 for key, (title, by) in m["ent"].items()]
        metas.append({"id": "tmdb:900001", "type": "movie", "name": "Synthetic Pick"})
        s.ANCHORS = tuple((key, "movie", "cozy_case", "proxy") for key in m["ent"])
        routes, decisions = 0, 0
        with patch.object(s, "row_metas", return_value=metas), patch.object(s, "row_list", return_value=metas), \
                patch.object(s, "feed_rows", return_value=[(cid, ("tv",), "AnaTitleSentinel") for cid in s.DEDUPE_ORDER]), \
                patch.object(s, "metrics", return_value={"rows": {}}):
            for viewer in (*s.PEOPLE, s.GUEST):
                forbidden = [title for title, by in m["ent"].values() if not s.visible_by(by, viewer)]
                for key, spec in s.API_ROUTES.items():
                    if "GET" not in spec["methods"]:
                        continue
                    query = {**spec["example"].get("query", {})}
                    if "person" in spec["query"]:
                        query["person"] = viewer
                    path = "/api/v1/"+key.replace("*", spec["example"].get("tail", ""))
                    if query:
                        path += "?"+urllib.parse.urlencode(query)
                    code, result = self.request(path, client=viewer)
                    self.assertIn(code, (200, 403, 404), (viewer, key, result))
                    text = json.dumps(result)
                    for title in forbidden:
                        self.assertNotIn(title, text, (viewer, key))
                    if viewer == s.GUEST and key == "rows" and code == 200:
                        self.assertLessEqual({r["id"] for r in result["rows"]}, s.GUEST_ROWS)
                    routes += 1
                for name, spec in s.DECISIONS.items():
                    if not spec["api"] or "person" not in spec["inp"]:
                        continue
                    for row in fixture_rows(s, name):
                        inp = {**copy.deepcopy(row["in"]), "person": viewer}
                        code, result = self.request("/api/v1/decide/"+name, "POST", {"in": inp}, client=viewer)
                        self.assertIn(code, (200, 403), (viewer, name, result))
                        for title in forbidden:
                            self.assertNotIn(title, json.dumps(result), (viewer, name))
                        if viewer == s.GUEST and code == 200:
                            answer = result["answer"]
                            self.assertIsNone(answer.get("match"))
                            self.assertIsNone(answer.get("why"))
                            self.assertEqual(answer.get("reasons", []), [])
                        decisions += 1
        print(f"Privacy: {routes} GET routes × viewers; {decisions} decision fixtures × viewers; 0 violations")


if __name__ == "__main__":
    unittest.main()
