"""Managed workers must split a long goal into small checkpointed turns."""
import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import managed


class WorkChunkingTests(unittest.TestCase):
    def test_worker_prompt_limits_each_call_to_one_bounded_action(self):
        prompt = json.loads(managed.work_prompt(
            {'config': {'goal': 'Complete a multi-step task'}},
            {'intend.md': 'Preserve the original goal.',
             'resume.md': 'Continue from the accepted checkpoint.',
             'todo.md': '1. Inspect input\n2. Implement\n3. Run checks',
             'quarantine.md': 'Do not retry rejected steps.'},
            {'run_id': 'run', 'turn_id': 'turn', 'origin_revision': 1,
             'generation': 1, 'artifact_sha256': 'sha'}))
        instructions = prompt['instructions'].lower()
        self.assertIn('exactly one pending item', instructions)
        self.assertIn('under 240 seconds', instructions)
        self.assertIn('one bounded action', instructions)
        self.assertIn('do not bundle later steps', instructions)
        self.assertIn('remaining substeps', instructions)
        self.assertIn('at most one concrete blocking question', instructions)
        self.assertEqual(len(prompt['context']), 4)


if __name__ == '__main__':
    unittest.main()
