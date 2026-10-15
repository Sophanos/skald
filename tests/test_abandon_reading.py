"""Skald's reading of started-then-stopped titles, from synthetic facets only."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import write_legacy, load_server, close_server


def rec(title, d=(), a=(), k=(), g=(), collection=None):
    return {"title": title, "kind": "movie", "y": "2020", "d": [[i, f"Director {i}"] for i in d],
            "a": [[i, f"Actor {i}"] for i in a], "k": [[i, f"keyword {i}"] for i in k], "g": list(g),
            "collection": collection}


class AbandonReadingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        write_legacy(Path(temp.name))
        self.s = load_server(Path(temp.name))
        self.addCleanup(close_server, self.s)
        self.ana, self.ben = self.s.PEOPLE[0], self.s.PEOPLE[1]
        desert = dict(k=(1, 2, 3), g=("Science Fiction", "Adventure"))
        self.s._facets.update({
            "tmdb:1": rec("Synthetic desert", d=(7,), a=(70, 71, 72), collection=700, **desert),
            "tmdb:2": rec("Synthetic dunes", d=(8,), a=(80, 72), **desert),
            "tmdb:4": rec("Synthetic favourite", d=(4,), a=(400,), k=(70,), g=("Western",)),
            "tmdb:3": rec("Synthetic sands", d=(9,), a=(90,), **desert),
            "tmdb:10": rec("Same director", d=(7,), a=(10,), k=(40,), g=("Drama",)),
            "tmdb:11": rec("Same saga", d=(11,), a=(11,), k=(41,), g=("Drama",), collection=700),
            "tmdb:12": rec("Lead of one watch", d=(12,), a=(71, 12), k=(42,), g=("Drama",)),
            "tmdb:20": rec("Lead of two watches", d=(20,), a=(72, 20), k=(43,), g=("Drama",)),
            "tmdb:21": rec("Lead of a favourite", d=(21,), a=(400, 21), k=(44,), g=("Drama",)),
            "tmdb:13": rec("Close in taste", d=(13,), a=(13,), **desert),
            "tmdb:14": rec("Far from taste", d=(14,), a=(14,), k=(50, 51), g=("Family",)),
            "tmdb:15": rec("Pressed abandon", d=(7,), a=(15,), **desert),
            "tmdb:17": rec("Stopped twin one", d=(99,), a=(17,), k=(60,), g=("Horror",)),
            "tmdb:18": rec("Stopped twin two", d=(99,), a=(18,), k=(60,), g=("Horror",)),
        })

    def entry(self, n, signal, weight, by=None, who="owner"):
        return {"key": f"tmdb:{n}", "id": n, "kind": "movie", "stype": "movie", "title": f"Synthetic {n}",
                "year": "2020", "signal": signal, "weight": weight, "age": 10, "item": {}, "who": who,
                "by": [by or self.ana], "flags": [], "genres": []}

    def test_stopped_titles_read_by_ties_and_taste(self):
        watched = [self.entry(n, "watched", 2.0) for n in (1, 2, 3)] + [self.entry(4, "loved", 3.0)]
        stopped = {n: self.entry(n, "abandoned", -1.5) for n in (10, 11, 12, 13, 14, 16, 17, 18, 20, 21)}
        pressed = self.entry(15, "abandoned", -1.5, who="remote")
        other = self.entry(19, "abandoned", -1.5, by=self.ben)
        self.s._facets["tmdb:19"] = rec("Other person", d=(7,), a=(19,), **{"k": (1, 2, 3), "g": ("Science Fiction",)})
        entries = watched + list(stopped.values()) + [pressed, other]
        self.s.read_abandoned(entries)
        read = {e["id"]: (e["signal"], e["weight"], e.get("abandon")) for e in entries}
        self.assertEqual(read[10], ("interest", .5, "related"))
        self.assertEqual(read[11], ("interest", .5, "related"))
        # A lead actor ties only through two watched titles or one loved title.
        self.assertEqual(read[12], ("abandoned", -.5, "far"))
        self.assertEqual(read[20], ("interest", .5, "related"))
        self.assertEqual(read[21], ("interest", .5, "related"))
        self.assertEqual(read[13], ("abandoned", 0.0, "unsure"))
        self.assertEqual(read[14], ("abandoned", -.5, "far"))
        self.assertEqual(read[16], ("abandoned", -.5, "far"))  # no facets yet
        # Ties come from watched and loved titles only, so two stopped films never vouch for each other.
        self.assertEqual(read[17], ("abandoned", -.5, "far"))
        self.assertEqual(read[18], ("abandoned", -.5, "far"))
        self.assertEqual(read[15], ("abandoned", -1.5, None))
        # Another member's watches do not explain this member's stop.
        self.assertEqual(read[19], ("abandoned", -.5, "far"))
        self.assertEqual([read[n] for n in (1, 2, 3)], [("watched", 2.0, None)] * 3)

    def test_stopped_titles_never_explain_a_pick(self):
        entries = [self.entry(n, "watched", 2.0) for n in (1, 2, 3)] + [self.entry(10, "abandoned", -1.5)]
        self.s.read_abandoned(entries)
        self.assertEqual(entries[-1]["abandon"], "related")
        profile = {"entries": entries, "genres": {}, "searches": [], "likes": "", "seen": set(), "negative": set()}
        with patch.object(self.s, "ensure_facets"), patch.object(self.s, "ensure_emb"):
            m = self.s.build_model(profile, enrich=False)
        self.assertNotIn("tmdb:10", m["ent"])
        self.assertFalse([fid for fid, hs in m["holders"].items() if any(k == "tmdb:10" for _, k in hs)])

    def test_member_opinions_on_shared_titles_are_read_per_member(self):
        watched = [self.entry(n, "watched", 2.0, by=self.ben) for n in (1, 2, 3)]
        shared = {**self.entry(10, "watched", 2.0, by=self.ben), "by": [self.ana, self.ben],
                  "opinions": {self.ana: {"signal": "abandoned", "weight": -1.5, "item": {}, "flags": [], "wsrc": "trakt"},
                               self.ben: {"signal": "watched", "weight": 2.0, "item": {}, "flags": [], "wsrc": "trakt"}}}
        self.s.read_abandoned(watched + [shared])
        self.assertEqual(shared["opinions"][self.ana]["weight"], -.5)
        self.assertEqual(shared["opinions"][self.ana]["abandon"], "far")
        self.assertEqual(shared["opinions"][self.ben]["signal"], "watched")


if __name__ == "__main__":
    unittest.main()
