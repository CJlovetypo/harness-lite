from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

from scripts import harness_train as train
from scripts.tests.harness_authoritative_fixture import AuthoritativeIntegrationFixture


class AuthorityValidationCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AuthoritativeIntegrationFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_candidate_cache_is_scoped_to_one_exact_snapshot(self) -> None:
        calls = 0
        original = subprocess.run

        def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        subprocess.run = counted
        try:
            with train.authority_validation_context(self.fixture.root) as context:
                first, first_blockers = train.load_registered_candidate(
                    self.fixture.root,
                    iteration="001",
                    generation="g1",
                    current_principle_sha256=self.fixture.principle_sha256,
                )
                before_second = calls
                second, second_blockers = train.load_registered_candidate(
                    self.fixture.root,
                    iteration="001",
                    generation="g1",
                    current_principle_sha256=self.fixture.principle_sha256,
                )
                after_second = calls
                self.assertTrue(context.candidate_cache)
        finally:
            subprocess.run = original

        self.assertIsNotNone(first)
        self.assertEqual(first_blockers, ())
        self.assertEqual(second, first)
        self.assertEqual(second_blockers, ())
        self.assertEqual(after_second, before_second)

    def test_ref_drift_before_return_fails_closed(self) -> None:
        candidate_ref = self.fixture.registered_candidate.candidate_ref
        with self.assertRaisesRegex(train.TrainError, "Git refs changed"):
            with train.authority_validation_context(self.fixture.root):
                loaded, blockers = train.load_registered_candidate(
                    self.fixture.root,
                    iteration="001",
                    generation="g1",
                    current_principle_sha256=self.fixture.principle_sha256,
                )
                self.assertIsNotNone(loaded)
                self.assertEqual(blockers, ())
                self.fixture.git(
                    "update-ref",
                    candidate_ref,
                    self.fixture.base_commit,
                    self.fixture.registered_candidate.candidate_commit,
                )

    @unittest.skipUnless(os.name == "nt", "Windows byte-lock regression")
    def test_current_operation_lock_is_not_read_as_authority(self) -> None:
        import msvcrt

        repo = train.open_repository(self.fixture.root)
        lock = (
            repo.common_dir
            / "project-harness"
            / "lifecycle"
            / "v2"
            / "locks"
            / "OP-self-lock-regression.lock"
        )
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                with train.authority_validation_context(self.fixture.root):
                    loaded, blockers = train.load_registered_candidate(
                        self.fixture.root,
                        iteration="001",
                        generation="g1",
                        current_principle_sha256=self.fixture.principle_sha256,
                    )
                    self.assertIsNotNone(loaded)
                    self.assertEqual(blockers, ())
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == "__main__":
    unittest.main()
