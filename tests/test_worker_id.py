from __future__ import annotations

import unittest
from unittest.mock import patch
from uuid import UUID

from worker import worker_id


class UploadWorkerIdTests(unittest.TestCase):
    def test_instances_differ_with_same_hostname_and_pid(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch(
            "worker.worker_id.socket.gethostname",
            return_value="onepiecenas",
        ), patch(
            "worker.worker_id.os.getpid",
            return_value=1,
        ), patch(
            "worker.worker_id.uuid.uuid4",
            side_effect=[UUID(int=1), UUID(int=2)],
        ):
            first = worker_id._create_upload_worker_id()
            second = worker_id._create_upload_worker_id()

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("UploadWorker-onepiecenas-1-"))
        self.assertTrue(second.startswith("UploadWorker-onepiecenas-1-"))

    def test_resolved_id_is_stable_for_process_lifetime(self) -> None:
        first = worker_id.resolve_upload_worker_id()
        second = worker_id.resolve_upload_worker_id()

        self.assertEqual(first, second)

    def test_configured_worker_id_remains_backward_compatible(self) -> None:
        with patch.dict(
            "os.environ",
            {"UPLOAD_WORKER_ID": "UploadWorker-explicit"},
            clear=True,
        ):
            resolved = worker_id._create_upload_worker_id()

        self.assertEqual(resolved, "UploadWorker-explicit")


if __name__ == "__main__":
    unittest.main()
