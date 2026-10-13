import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "app" / "server.py"
SPEC = importlib.util.spec_from_file_location("disc_ingest_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


SAMPLE = '''
CINFO:1,6206,"DVD disc"
CINFO:2,0,"A Great Film"
TCOUNT:4
TINFO:0,8,0,"20"
TINFO:0,9,0,"1:42:03"
TINFO:0,10,0,"5.9 GB"
TINFO:0,11,0,"6335076761"
TINFO:0,27,0,"title00.mkv"
TINFO:1,8,0,"6"
TINFO:1,9,0,"0:24:12"
TINFO:1,10,0,"1.1 GB"
TINFO:1,11,0,"1181116006"
TINFO:2,8,0,"6"
TINFO:2,9,0,"0:23:51"
TINFO:2,10,0,"1.0 GB"
TINFO:2,11,0,"1073741824"
TINFO:3,8,0,"2"
TINFO:3,9,0,"0:03:12"
TINFO:3,10,0,"120 MB"
TINFO:3,11,0,"125829120"
'''


class ParserTests(unittest.TestCase):
    def test_parses_disc_and_title_defaults(self):
        disc = server.parse_disc_info(SAMPLE)
        self.assertIsNotNone(disc)
        self.assertEqual(disc.name, "A Great Film")
        self.assertEqual(len(disc.titles), 4)
        self.assertEqual(disc.movie_default, [0])
        self.assertEqual(disc.show_default, [1, 2])
        self.assertEqual(disc.titles[0].seconds, 6123)
        self.assertEqual(disc.titles[0].chapters, 20)

    def test_no_titles_means_no_disc(self):
        self.assertIsNone(server.parse_disc_info('MSG:5010,0,0,"No disc inserted"'))

    def test_duration_parser(self):
        self.assertEqual(server.duration_seconds("1:02:03"), 3723)
        self.assertEqual(server.duration_seconds("24:12"), 1452)
        self.assertEqual(server.duration_seconds("unknown"), 0)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.disc = server.parse_disc_info(SAMPLE)
        self.assertIsNotNone(self.disc)
        self.previous_root = server.MEDIA_ROOT
        self.temp = tempfile.TemporaryDirectory()
        server.MEDIA_ROOT = Path(self.temp.name).resolve()

    def tearDown(self):
        server.MEDIA_ROOT = self.previous_root
        self.temp.cleanup()

    def test_movie_plan_uses_jellyfin_naming(self):
        plan = server.build_rip_plan(
            {"media_type": "movie", "name": "A Great Film", "year": "1999", "title_ids": [0]},
            self.disc,
        )
        self.assertEqual(plan["destination"], server.MEDIA_ROOT / "Movies" / "A Great Film (1999)")
        self.assertEqual(plan["filenames"], ["A Great Film (1999).mkv"])

    def test_show_plan_numbers_episodes(self):
        plan = server.build_rip_plan(
            {
                "media_type": "show",
                "name": "A Great Show",
                "season": 2,
                "starting_episode": 3,
                "title_ids": [1, 2],
            },
            self.disc,
        )
        self.assertEqual(plan["destination"], server.MEDIA_ROOT / "Shows" / "A Great Show" / "Season 02")
        self.assertEqual(
            plan["filenames"],
            ["A Great Show - S02E03.mkv", "A Great Show - S02E04.mkv"],
        )

    def test_path_traversal_is_neutralized(self):
        plan = server.build_rip_plan(
            {"media_type": "movie", "name": "../../Outside", "year": "", "title_ids": [0]},
            self.disc,
        )
        self.assertEqual(plan["destination"], server.MEDIA_ROOT / "Movies" / "Outside")

    def test_movie_rejects_multiple_titles(self):
        with self.assertRaises(server.ValidationError):
            server.build_rip_plan(
                {"media_type": "movie", "name": "A Great Film", "title_ids": [0, 1]},
                self.disc,
            )

    def test_existing_file_is_not_overwritten(self):
        destination = server.MEDIA_ROOT / "Movies" / "A Great Film"
        destination.mkdir(parents=True)
        (destination / "A Great Film.mkv").touch()
        with self.assertRaises(server.ValidationError):
            server.build_rip_plan(
                {"media_type": "movie", "name": "A Great Film", "title_ids": [0]},
                self.disc,
            )


class ControllerTests(unittest.TestCase):
    def test_automatic_eject_failure_is_only_a_warning(self):
        previous = server.EJECT_BIN
        server.EJECT_BIN = "/definitely/missing/eject"
        controller = server.DiscController()
        try:
            controller._eject(log_failure=True)
        finally:
            server.EJECT_BIN = previous
        self.assertIn("Automatic eject failed", controller.logs[-1]["message"])


if __name__ == "__main__":
    unittest.main()
