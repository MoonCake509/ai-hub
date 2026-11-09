import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hub'))
from app import db


class ReportedModelPersistence(unittest.TestCase):
    def persisted(self, usage):
        conn = Mock()
        rows = [dict(status='running', job_id='job', attempt_no=1),
                dict(status='running', id='job', requested_model='requested-only'), dict(n=1)]
        with patch.object(db, 'connect', return_value=conn), patch.object(db, '_one', side_effect=rows):
            self.assertEqual(db.finish_attempt('attempt', True, 'answer', None, usage, 1), 'applied')
        updates = [call.kwargs['em'] for call in conn.run.call_args_list if 'em' in call.kwargs]
        self.assertEqual(len(updates), 1)
        return updates[0]

    def test_reported_model_is_persisted(self):
        self.assertEqual(self.persisted({'reported_model': ' xiaomi-ocr-q8 '}), 'xiaomi-ocr-q8')

    def test_legacy_model_fallback(self):
        self.assertEqual(self.persisted({'reported_model': {}, 'model': 'legacy-model'}), 'legacy-model')

    def test_missing_or_invalid_model_never_uses_requested_model(self):
        for usage in ({}, {'reported_model': '', 'model': []}):
            with self.subTest(usage=usage):
                self.assertIsNone(self.persisted(usage))
