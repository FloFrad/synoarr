from __future__ import annotations

import unittest

from synoarr.config import QualityConfig
from synoarr.models import Release, SearchRequest
from synoarr.quality import evaluate, pick_best, rank_releases

GB = 1024 ** 3
MB = 1024 ** 2


def rel(title, seeders=50, size=4 * GB, **kw):
    return Release(title=title, indexer="test", download_url="http://x/%s.torrent" % title.replace(" ", "."),
                   seeders=seeders, size=size, **kw)


MOVIE = SearchRequest(kind="movie", title="Dune", year=2021)
DUNE2 = SearchRequest(kind="movie", title="Dune: Part Two", year=2024)
EPISODE = SearchRequest(kind="episode", title="The Last of Us", season=1, episode=3)
SEASON = SearchRequest(kind="season", title="The Last of Us", season=1)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = QualityConfig(languages=["en", "fr"], prefer_words=[])  # 1080p, 720p ; en, fr

    def score(self, title, request=MOVIE, **kw):
        return evaluate(rel(title, **kw), request, self.cfg)

    def assertAccepted(self, title, request=MOVIE, **kw):
        s, reason = self.score(title, request, **kw)
        self.assertIsNotNone(s, "%s rejected: %s" % (title, reason))
        return s

    def assertRejected(self, title, request=MOVIE, contains=None, **kw):
        s, reason = self.score(title, request, **kw)
        self.assertIsNone(s, "%s accepted (%s)" % (title, reason))
        self.assertTrue(reason)
        if contains:
            self.assertIn(contains, reason)
        return reason

    # --- languages
    def test_languages(self):
        self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP")          # no tag = English
        self.assertAccepted("Dune.2021.MULTI.1080p.BluRay.x264-GRP")
        self.assertAccepted("Dune.2021.TRUEFRENCH.1080p.BluRay.x264-GRP")
        self.assertAccepted("Dune.2021.FRENCH.1080p.WEB-DL.x264-GRP")
        # VOSTFR = burned-in French subtitles: only when "vostfr" is explicitly wanted
        self.assertRejected("Dune.2021.VOSTFR.1080p.WEB.x264-GRP", contains="langue")
        self.cfg.languages = ["en", "vostfr"]
        self.assertAccepted("Dune.2021.VOSTFR.1080p.WEB.x264-GRP")
        self.cfg.languages = ["en", "fr"]
        self.assertRejected("Dune.2021.iTALiAN.1080p.BluRay.x264-GRP", contains="langue")
        self.assertRejected("Dune.2021.German.1080p.BluRay.x264-GRP", contains="langue")

    def test_english_default_and_animation_override(self):
        cfg = QualityConfig()  # new defaults: en ; animation fr
        self.assertEqual(cfg.languages, ["en"])
        movie = SearchRequest(kind="movie", title="Dune", year=2021)
        self.assertIsNotNone(evaluate(rel("Dune.2021.1080p.BluRay.x264-GRP"), movie, cfg)[0])
        self.assertIsNotNone(evaluate(rel("Dune.2021.MULTi.1080p.BluRay.x264-GRP"), movie, cfg)[0])
        self.assertIsNone(evaluate(rel("Dune.2021.TRUEFRENCH.1080p.BluRay.x264-GRP"), movie, cfg)[0])
        self.assertIsNone(evaluate(rel("Dune.2021.VOSTFR.1080p.WEB.x264-GRP"), movie, cfg)[0])
        # English subtitles preferred
        a = evaluate(rel("Dune.2021.1080p.WEB-DL.MultiSubs.x264-GRP"), movie, cfg)[0]
        b = evaluate(rel("Dune.2021.1080p.WEB-DL.x264-GRP"), movie, cfg)[0]
        self.assertGreater(a, b)
        cartoon = SearchRequest(kind="movie", title="Coco", year=2017, languages=list(cfg.languages_animation))
        self.assertIsNone(evaluate(rel("Coco.2017.1080p.BluRay.x264-GRP"), cartoon, cfg)[0])
        self.assertIsNotNone(evaluate(rel("Coco.2017.TRUEFRENCH.1080p.BluRay.x264-GRP"), cartoon, cfg)[0])
        self.assertIsNotNone(evaluate(rel("Coco.2017.MULTi.VFF.1080p.BluRay.x264-GRP"), cartoon, cfg)[0])

    def test_language_only_french(self):
        self.cfg.languages = ["fr"]
        self.assertRejected("Dune.2021.1080p.BluRay.x264-GRP")
        self.assertRejected("Dune.2021.VOSTFR.1080p.WEB.x264-GRP")
        self.assertAccepted("Dune.2021.MULTI.1080p.BluRay.x264-GRP")
        self.assertAccepted("Dune.2021.FRENCH.1080p.BluRay.x264-GRP")

    def test_language_preference_order(self):
        en = self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP")
        fr = self.assertAccepted("Dune.2021.FRENCH.1080p.BluRay.x264-GRP")
        multi = self.assertAccepted("Dune.2021.MULTI.1080p.BluRay.x264-GRP")
        self.assertGreater(en, fr)
        self.assertGreater(multi, en)
        self.cfg.languages = ["fr", "en"]
        en = self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP")
        fr = self.assertAccepted("Dune.2021.FRENCH.1080p.BluRay.x264-GRP")
        self.assertGreater(fr, en)

    def test_no_language_filter(self):
        self.cfg.languages = []
        self.assertAccepted("Dune.2021.iTALiAN.1080p.BluRay.x264-GRP")

    # --- resolutions
    def test_resolutions(self):
        s1080 = self.assertAccepted("Dune.2021.1080p.WEB-DL.x264-GRP")
        s720 = self.assertAccepted("Dune.2021.720p.BluRay.x264-GRP")
        self.assertGreater(s1080, s720)  # resolution beats source
        self.assertRejected("Dune.2021.2160p.UHD.BluRay.x265-GRP", contains="2160p")
        self.assertRejected("Dune.2021.480p.DVDRip.x264-GRP", contains="480p")
        unknown = self.assertAccepted("Dune.2021.BluRay.x264-GRP")
        self.assertLess(unknown, s720)

    def test_allow_other_resolutions(self):
        self.cfg.allow_other_resolutions = True
        s2160 = self.assertAccepted("Dune.2021.2160p.UHD.BluRay.x265-GRP")
        s720 = self.assertAccepted("Dune.2021.720p.BluRay.x264-GRP")
        self.assertLess(s2160, s720)

    def test_resolution_beats_language(self):
        multi720 = self.assertAccepted("Dune.2021.MULTI.720p.BluRay.x264-GRP")
        fr1080 = self.assertAccepted("Dune.2021.FRENCH.1080p.HDTV.x264-GRP")
        self.assertGreater(fr1080, multi720)

    # --- bad sources / reject words
    def test_cam_and_telesync_rejected(self):
        self.assertRejected("Dune.2021.CAM.x264-GRP", contains="CAM")
        self.assertRejected("Dune.2021.HDCAM.720p.x264-GRP")
        self.assertRejected("Dune.2021.HDTS.720p.x264-GRP")
        self.assertRejected("Dune.2021.720p.TS")
        self.assertRejected("Dune.2021.1080p.WEB-DL.SCREENER.x264")

    def test_reject_word_not_applied_to_title(self):
        req = SearchRequest(kind="movie", title="Cam", year=2018)
        self.assertAccepted("Cam.2018.1080p.WEB-DL.x264-GRP", request=req)
        self.assertRejected("Cam.2018.HDCAM.x264-GRP", request=req)

    def test_custom_reject_words(self):
        self.cfg.reject_words = ["hdr", "3d"]
        self.assertRejected("Dune.2021.1080p.BluRay.HDR.x265-GRP", contains="hdr")
        self.assertRejected("Dune.2021.3D.1080p.BluRay.x264-GRP")
        self.assertAccepted("Dune.2021.1080p.BluRay.x264-HDRGRP")

    def test_dts_audio_is_not_telesync(self):
        self.assertAccepted("Dune.2021.1080p.BluRay.DTS-HD.MA.5.1.x264-GRP")

    # --- titles and years
    def test_wrong_title(self):
        self.assertRejected("Dune.Part.Two.2024.1080p.WEB-DL.x264-GRP", contains="ne correspond pas")
        self.assertRejected("Dune.2021.1080p.BluRay.x264-GRP", request=DUNE2)
        self.assertAccepted("Dune.Part.Two.2024.1080p.WEB-DL.x264-GRP", request=DUNE2)

    def test_alt_titles(self):
        req = SearchRequest(kind="movie", title="Le Fabuleux Destin d'Amélie Poulain", year=2001,
                            alt_titles=["Amelie"])
        self.assertAccepted("Amelie.2001.1080p.BluRay.x264-GRP", request=req)
        self.assertAccepted("Le.Fabuleux.Destin.d.Amelie.Poulain.2001.MULTI.1080p.BluRay", request=req)

    def test_year(self):
        self.assertRejected("Dune.1984.1080p.BluRay.x264-GRP", contains="année")
        self.assertAccepted("Dune.2020.1080p.BluRay.x264-GRP")   # +-1 tolerated
        self.assertAccepted("Dune.2022.1080p.BluRay.x264-GRP")
        self.assertAccepted("Dune.1080p.BluRay.x264-GRP")         # unknown year accepted
        no_year = SearchRequest(kind="movie", title="Dune")
        self.assertAccepted("Dune.1984.1080p.BluRay.x264-GRP", request=no_year)

    def test_movie_rejects_episode_markers(self):
        self.assertRejected("Dune.S01E01.1080p.WEB-DL.x264-GRP", contains="saison")

    # --- series
    def test_episode(self):
        self.assertAccepted("The.Last.of.Us.S01E03.1080p.WEB-DL.x264-GRP", request=EPISODE, size=2 * GB)
        self.assertAccepted("The.Last.of.Us.S01E02E03.720p.HDTV.x264-GRP", request=EPISODE, size=2 * GB)
        self.assertRejected("The.Last.of.Us.S01E04.1080p.WEB-DL.x264-GRP", request=EPISODE, size=2 * GB)
        self.assertRejected("The.Last.of.Us.S02E03.1080p.WEB-DL.x264-GRP", request=EPISODE, size=2 * GB)
        self.assertRejected("The.Last.of.Us.S01.1080p.WEB-DL.x264-GRP", request=EPISODE, size=2 * GB,
                            contains="pack")
        self.assertRejected("The.Last.of.Us.2023.1080p.WEB-DL.x264-GRP", request=EPISODE, size=2 * GB)

    def test_season_pack(self):
        self.assertAccepted("The.Last.of.Us.S01.1080p.WEB-DL.x264-GRP", request=SEASON, size=30 * GB)
        self.assertAccepted("The.Last.of.Us.Season.1.MULTI.1080p.WEB-DL.x264-GRP", request=SEASON, size=30 * GB)
        self.assertRejected("The.Last.of.Us.S01E03.1080p.WEB-DL.x264-GRP", request=SEASON, size=2 * GB)
        self.assertRejected("The.Last.of.Us.S02.1080p.WEB-DL.x264-GRP", request=SEASON, size=30 * GB)
        self.assertRejected("The.Last.of.Us.S01.1080p.WEB-DL.x264-GRP", request=SEASON, size=80 * GB,
                            contains="trop grande")

    def test_multi_season_pack_contains_season(self):
        req = SearchRequest(kind="season", title="The Last of Us", season=2)
        self.assertAccepted("The.Last.of.Us.S01-S03.1080p.WEB-DL.x264-GRP", request=req, size=50 * GB)
        req4 = SearchRequest(kind="season", title="The Last of Us", season=4)
        self.assertRejected("The.Last.of.Us.S01-S03.1080p.WEB-DL.x264-GRP", request=req4, size=50 * GB)

    def test_season_discovery_without_season(self):
        req = SearchRequest(kind="season", title="The Last of Us")
        self.assertAccepted("The.Last.of.Us.S02.1080p.WEB-DL.x264-GRP", request=req, size=30 * GB)
        self.assertRejected("The.Last.of.Us.S02E01.1080p.WEB-DL.x264-GRP", request=req, size=3 * GB)

    # --- seeders / size / sample
    def test_seeders(self):
        self.assertRejected("Dune.2021.1080p.BluRay.x264-GRP", seeders=0, contains="seeders")
        self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP", seeders=1)
        self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP", seeders=None)  # unknown (webseeds)
        self.cfg.min_seeders = 5
        self.assertRejected("Dune.2021.1080p.BluRay.x264-GRP", seeders=4)
        many = self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP", seeders=500)
        few = self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP", seeders=5)
        self.assertGreater(many, few)

    def test_size_limits(self):
        self.assertRejected("Dune.2021.1080p.BluRay.x264-GRP", size=50 * MB, contains="petite")
        self.assertRejected("Dune.2021.1080p.BluRay.x264-GRP", size=30 * GB, contains="grande")
        self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP", size=0)  # unknown size
        self.assertRejected("The.Last.of.Us.S01E03.1080p.WEB-DL.x264-GRP", request=EPISODE, size=8 * GB)
        self.cfg.max_size_gb_movie = 0  # 0 = no maximum
        self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP", size=80 * GB)

    def test_sample_rejected(self):
        self.assertRejected("Dune.2021.1080p.BluRay.x264-GRP.sample", contains="sample")

    def test_source_ranking(self):
        bluray = self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP")
        webdl = self.assertAccepted("Dune.2021.1080p.WEB-DL.x264-GRP")
        hdtv = self.assertAccepted("Dune.2021.1080p.HDTV.x264-GRP")
        self.assertGreater(bluray, webdl)
        self.assertGreater(webdl, hdtv)

    def test_prefer_words(self):
        self.cfg.prefer_words = ["remux"]
        a = self.assertAccepted("Dune.2021.1080p.BluRay.REMUX.x264-GRP", size=20 * GB)
        b = self.assertAccepted("Dune.2021.1080p.BluRay.x264-GRP", size=20 * GB)
        self.assertGreater(a, b)

    def test_empty_title(self):
        self.assertIsNone(evaluate(rel(""), MOVIE, self.cfg)[0])


class PickBestTests(unittest.TestCase):
    def setUp(self):
        self.cfg = QualityConfig(languages=["en", "fr"], prefer_words=[])

    def test_pick_best(self):
        releases = [
            rel("Dune.2021.2160p.UHD.BluRay.x265-GRP", seeders=900),
            rel("Dune.2021.720p.BluRay.x264-GRP", seeders=300),
            rel("Dune.2021.MULTI.1080p.WEB-DL.x264-GRP", seeders=40),
            rel("Dune.2021.iTALiAN.1080p.BluRay.x264-GRP", seeders=999),
            rel("Dune.2021.CAM.x264-GRP", seeders=2000),
            rel("Dune.Part.Two.2024.1080p.BluRay.x264-GRP", seeders=5000),
        ]
        best = pick_best(releases, MOVIE, self.cfg)
        self.assertEqual(best.title, "Dune.2021.MULTI.1080p.WEB-DL.x264-GRP")

    def test_none_when_nothing_acceptable(self):
        self.assertIsNone(pick_best([rel("Dune.2021.CAM.x264-GRP")], MOVIE, self.cfg))
        self.assertIsNone(pick_best([], MOVIE, self.cfg))

    def test_blocklist(self):
        releases = [rel("Dune.2021.1080p.BluRay.x264-GRP", seeders=100),
                    rel("Dune.2021.1080p.WEB-DL.x264-GRP", seeders=100)]
        blocked = {"dune.2021.1080p.bluray.x264-grp"}
        best = pick_best(releases, MOVIE, self.cfg, is_blocked=lambda t: t.lower() in blocked)
        self.assertEqual(best.title, "Dune.2021.1080p.WEB-DL.x264-GRP")

    def test_broken_blocklist_callback(self):
        def boom(title):
            raise RuntimeError("db locked")
        with self.assertLogs("synoarr.quality", level="WARNING"):
            best = pick_best([rel("Dune.2021.1080p.BluRay.x264-GRP")], MOVIE, self.cfg, is_blocked=boom)
        self.assertIsNotNone(best)

    def test_tie_more_seeders(self):
        a = rel("Dune.2021.1080p.BluRay.x264-AAA", seeders=10000)
        b = rel("Dune.2021.1080p.BluRay.x264-BBB", seeders=20000)  # both capped in log score
        self.assertEqual(pick_best([a, b], MOVIE, self.cfg).title, b.title)

    def test_duplicate_info_hash(self):
        a = rel("Dune.2021.1080p.BluRay.x264-AAA", info_hash="a" * 40)
        b = rel("Dune.2021.1080p.BluRay.x264-AAA", info_hash="A" * 40)
        self.assertEqual(len(rank_releases([a, b], MOVIE, self.cfg)), 1)

    def test_episode_prefers_1080p(self):
        releases = [rel("The.Last.of.Us.S01E03.720p.WEB-DL.x264-GRP", seeders=500, size=1 * GB),
                    rel("The.Last.of.Us.S01E03.1080p.WEB-DL.x264-GRP", seeders=20, size=3 * GB),
                    rel("The.Last.of.Us.S01.1080p.WEB-DL.x264-GRP", seeders=900, size=30 * GB)]
        self.assertEqual(pick_best(releases, EPISODE, self.cfg).title, "The.Last.of.Us.S01E03.1080p.WEB-DL.x264-GRP")

    def test_ia_style_release(self):
        req = SearchRequest(kind="movie", title="Night of the Living Dead", year=1968)
        r = Release(title="Night of the Living Dead (1968)", indexer="ia", download_url="http://x", seeders=None,
                    size=900 * MB)
        self.assertIsNotNone(pick_best([r], req, self.cfg))


    def test_title_containing_language_word(self):
        req = SearchRequest(kind="movie", title="The French Dispatch", year=2021)
        s, reason = evaluate(rel("The.French.Dispatch.2021.1080p.WEB-DL.DDP5.1.H.264-EVO"), req, self.cfg)
        self.assertIsNotNone(s, reason)
        self.assertIn("VO", reason)  # "French" belongs to the title, not a language tag
        s, reason = evaluate(rel("The.French.Dispatch.2021.MULTi.VFF.1080p.WEB"), req, self.cfg)
        self.assertIn("multi", reason)

    def test_number_in_title_is_not_a_year(self):
        br = SearchRequest(kind="movie", title="Blade Runner 2049", year=2017)
        self.assertIsNotNone(evaluate(rel("Blade.Runner.2049.1080p.BluRay.x264-SPARKS"), br, self.cfg)[0])
        self.assertIsNotNone(evaluate(rel("Blade.Runner.2049.2017.1080p.BluRay.x264"), br, self.cfg)[0])
        old = SearchRequest(kind="movie", title="Blade Runner", year=1982)
        self.assertIsNone(evaluate(rel("Blade.Runner.2049.1080p.BluRay.x264-SPARKS"), old, self.cfg)[0])
        self.assertIsNotNone(evaluate(rel("Blade.Runner.1982.Final.Cut.1080p.BluRay"), old, self.cfg)[0])
        self.assertIsNotNone(evaluate(rel("1917.2019.1080p.BluRay.x264"),
                                      SearchRequest(kind="movie", title="1917", year=2019), self.cfg)[0])

    def test_prefix_match_does_not_accept_sequels(self):
        self.assertIsNone(evaluate(rel("Dune.Part.Two.2024.1080p.WEB"), MOVIE, self.cfg)[0])
        ted = SearchRequest(kind="movie", title="Ted", year=2012)
        self.assertIsNone(evaluate(rel("Ted.2.2015.1080p.BluRay.x264"), ted, self.cfg)[0])

    def test_apostrophes_accents_and_site_tags(self):
        req = SearchRequest(kind="movie", title="Le Fabuleux Destin d'Amélie Poulain", year=2001)
        for t in ("Le.Fabuleux.Destin.d.Amelie.Poulain.2001.FRENCH.1080p.BluRay.x264",
                  "[ OxTorrent.com ] Le.Fabuleux.Destin.d'Amelie.Poulain.2001.FRENCH.1080p"):
            self.assertIsNotNone(evaluate(rel(t), req, self.cfg)[0], t)
        greys = SearchRequest(kind="episode", title="Grey's Anatomy", season=20, episode=3)
        self.assertIsNotNone(evaluate(rel("Greys.Anatomy.S20E03.1080p.WEB.h264", size=GB), greys, self.cfg)[0])


if __name__ == "__main__":
    unittest.main()


class IndexerPriorityTests(unittest.TestCase):
    """Indexer priority (1 = most preferred .. 50) breaks ties but never beats resolution / language / source."""

    def setUp(self):
        self.cfg = QualityConfig(languages=["en", "fr"], prefer_words=[])
        self.prios = {"Favori": 1, "Moyen": 25, "Dernier": 50}

    @staticmethod
    def on(indexer, title, seeders=50, size=4 * GB):
        return Release(title=title, indexer=indexer, download_url="http://x/%s.torrent" % title,
                       seeders=seeders, size=size)

    def test_constants_keep_the_level_order(self):
        from synoarr import quality as q
        self.assertGreater((q.PRIORITY_MAX - q.PRIORITY_MIN) * q.PRIORITY_STEP, q.SEEDERS_MAX + 200)
        self.assertLess((q.PRIORITY_MAX - q.PRIORITY_MIN) * q.PRIORITY_STEP, q.SOURCE_WEIGHT)
        self.assertLess((q.PRIORITY_MAX - q.PRIORITY_MIN) * q.PRIORITY_STEP, q.PREFER_WORD_BONUS)

    def test_equal_quality_prefers_the_priority_indexer_over_seeders(self):
        a = self.on("Dernier", "Dune.2021.1080p.BluRay.x264-AAA", seeders=5000)
        b = self.on("Favori", "Dune.2021.1080p.BluRay.x264-BBB", seeders=3)
        self.assertEqual(pick_best([a, b], MOVIE, self.cfg, priorities=self.prios).title, b.title)
        # without priorities (old callers): seeders decide, no bonus
        self.assertEqual(pick_best([a, b], MOVIE, self.cfg).title, a.title)
        self.assertEqual(evaluate(b, MOVIE, self.cfg)[0] + 49 * 200,
                         evaluate(b, MOVIE, self.cfg, self.prios)[0])

    def test_priority_never_overrides_resolution_language_or_source(self):
        cases = [
            (self.on("Dernier", "Dune.2021.1080p.WEB-DL.x264-A"), self.on("Favori", "Dune.2021.720p.BluRay.x264-B")),
            (self.on("Dernier", "Dune.2021.1080p.WEB-DL.x264-A"), self.on("Favori", "Dune.2021.FRENCH.1080p.WEB-DL-B")),
            (self.on("Dernier", "Dune.2021.1080p.WEBRip.x264-A"), self.on("Favori", "Dune.2021.1080p.HDTV.x264-B")),
        ]
        for better, preferred_indexer in cases:
            with self.subTest(better=better.title):
                self.assertEqual(pick_best([preferred_indexer, better], MOVIE, self.cfg,
                                           priorities=self.prios).title, better.title)

    def test_unknown_indexer_counts_as_default_and_case_insensitive(self):
        from synoarr.quality import indexer_priority, priority_points
        self.assertEqual(indexer_priority("inconnu", self.prios), 25)
        self.assertEqual(indexer_priority("favori", self.prios), 1)
        self.assertIsNone(indexer_priority("Favori", None))
        self.assertEqual(priority_points("Dernier", self.prios), 0)
        self.assertEqual(priority_points("x", {"x": 999}), 0)  # clamped to 50
        self.assertEqual(priority_points("x", {"x": "abc"}), 25 * 200)
