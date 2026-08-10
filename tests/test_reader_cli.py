from __future__ import annotations

import unittest

from app.source_candidates import load_candidate_catalog
from reader.cli import _parser, _select_candidates, _validated_report_output


class ReaderCliSelectionTests(unittest.TestCase):
    def test_arbitrary_reviewed_handle_is_reachable(self) -> None:
        args = _parser().parse_args(
            ["resolve-sources", "--handle", "@BiznesFranchise"]
        )

        selected = _select_candidates(load_candidate_catalog(), args)

        self.assertEqual([candidate.handle for candidate in selected], ["BiznesFranchise"])

    def test_unknown_handle_is_rejected(self) -> None:
        args = _parser().parse_args(
            ["resolve-sources", "--handle", "not_in_catalog"]
        )

        with self.assertRaisesRegex(ValueError, "not present"):
            _select_candidates(load_candidate_catalog(), args)

    def test_report_cannot_overwrite_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "protected"):
            _validated_report_output(
                "./data/telegram/reader.session",
                protected_paths=("./data/telegram/reader.session",),
            )

    def test_report_requires_json_extension(self) -> None:
        with self.assertRaisesRegex(ValueError, "end with .json"):
            _validated_report_output(
                "./data/report.txt",
                protected_paths=(),
            )


if __name__ == "__main__":
    unittest.main()
