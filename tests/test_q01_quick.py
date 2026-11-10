"""Small dependency-free checks for Q-01 quick channel runs."""
import unittest

from app import quality, quality_db, quality_quick


class QuickProbes(unittest.TestCase):
    def test_text_quick_mode_is_three_short_unique_probes(self):
        suite = quality_quick.build_suite("a1B2-XX", False)
        self.assertEqual(len(suite["cases"]), 3)
        self.assertEqual(suite["max_calls"], 3)
        self.assertIn("QK-A1B2", suite["cases"][0]["prompt"])
        self.assertIn("KITE-A1B2", suite["cases"][2]["prompt"])
        self.assertNotIn("QK-A1B2-XX", suite["cases"][0]["prompt"])

    def test_vision_gets_a_real_png_not_a_text_benchmark(self):
        suite = quality_quick.build_suite("a1b2", True)
        self.assertEqual(len(suite["cases"]), 1)
        case = suite["cases"][0]
        image = quality_quick.ocr_fixture_png()
        self.assertEqual(case["fixture"]["kind"], "image/png")
        self.assertEqual(case["fixture"]["sha256_16"], quality_quick.ocr_fixture_sha())
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(quality.grade(case, "LUNA 73")["passed"], True)
        self.assertEqual(quality.grade(case, "luna\n73")["passed"], True)
        self.assertEqual(quality.grade(case, "LUNA 74")["passed"], False)

    def test_ocr_normalizer_does_not_lose_cyrillic(self):
        # Latin normalization is explicitly scoped to the Latin fixture. A
        # Russian regression fixture must remain nonempty after normalization.
        from app.quality import _normalize_ocr
        self.assertEqual(_normalize_ocr("ПОЕЗД 17"), "17")
        self.assertEqual(_normalize_ocr("LUNA-73"), "LUNA73")

    def test_budget_is_bounded(self):
        self.assertEqual(quality_quick.normalize_budget(None), 45)
        self.assertEqual(quality_quick.normalize_budget(60), 60)
        for invalid in (9, 121, "45", True):
            with self.assertRaises(ValueError):
                quality_quick.normalize_budget(invalid)

    def test_partial_run_does_not_count_skipped_as_failure(self):
        run = {"status": "completed", "items": [
            {"status": "succeeded", "passed": True, "case": {"title": "Инструкция"}},
            {"status": "budget_exhausted", "passed": None,
             "case": {"title": "JSON"}, "grader_reason": "Бюджет"},
            {"status": "not_applicable", "passed": None, "case": {"title": "OCR"}},
        ]}
        result = quality_quick.verdict(run)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["graded_count"], 1)
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual(result["failed_count"], 0)
        self.assertIn("JSON", " ".join(result["reasons"]))

    def test_no_completed_probe_is_insufficient_not_success(self):
        run = {"status": "completed", "items": [
            {"status": "budget_exhausted", "passed": None, "case": {"title": "Инструкция"}},
        ]}
        self.assertEqual(quality_quick.verdict(run)["state"], "insufficient")

    def test_expired_job_is_a_deviation_not_a_success(self):
        run = {"status": "completed", "items": [
            {"status": "timeout", "passed": False, "case": {"title": "OCR"},
             "grader_reason": "Таймаут"},
        ]}
        self.assertEqual(quality_quick.verdict(run)["state"], "deviations")

    def test_budget_stops_a_still_queued_probe(self):
        run = {"status": "running", "budget_s": 45, "items": []}
        self.assertFalse(quality_quick.budget_exhausted(run, 44))
        self.assertTrue(quality_quick.budget_exhausted(run, 46))
        self.assertIn("45", quality_quick.skip_reason(run))

    def test_quality_ddl_is_additive(self):
        statements = "\n".join(quality_db.DDL)
        self.assertIn("add column if not exists mode text", statements)
        self.assertIn("add column if not exists budget_s int", statements)
        self.assertIn("add column if not exists max_calls int", statements)
        self.assertIn("add column if not exists budget_exhausted boolean", statements)

    def test_public_run_exposes_budget_and_verdict(self):
        run = {"id": "r", "mode": "quick", "budget_s": 45, "max_calls": 3,
               "budget_exhausted": True, "status": "completed", "items": []}
        result = quality.public_run(run)
        self.assertEqual(result["mode"], "quick")
        self.assertEqual(result["budget_s"], 45)
        self.assertEqual(result["max_calls"], 3)
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["verdict"]["state"], "insufficient")


if __name__ == "__main__":
    unittest.main()
