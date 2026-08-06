"""MemoryKeeper PC Folder Watcher."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from watcher.file_hash_cache import FileHashCache
from watcher.upload_client import UploadClient, UploadClientError

logger = logging.getLogger(__name__)

DEFAULT_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "heic",
    "dng",
    "tif",
    "tiff",
    "mov",
    "mp4",
}

STABLE_INTERVAL_SECONDS = 2.0
STABLE_CHECKS = 3
FILE_LOCK_RETRIES = 5
FILE_LOCK_RETRY_DELAY = 1.0


def load_watch_config(config_path: str | Path) -> dict[str, Any]:
    """watch_config.json을 로드한다."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def setup_logging() -> None:
    """Watcher 로그 포맷을 설정한다."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class FolderWatchHandler(FileSystemEventHandler):
    """새 파일 이벤트를 처리한다."""

    def __init__(self, watcher: "FolderWatcher") -> None:
        super().__init__()
        self.watcher = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.watcher.enqueue(Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.watcher.enqueue(Path(str(event.dest_path)))


class FolderWatcher:
    """
    지정 폴더를 감시하고 신규 사진만 Upload API로 전송한다.

    원본 파일은 수정하지 않으며 copy2()로 임시 복사본만 업로드한다.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.watch_paths = [Path(path) for path in config.get("watch_paths", [])]
        self.recursive = bool(config.get("recursive", True))
        extensions = config.get("extensions") or sorted(DEFAULT_EXTENSIONS)
        self.extensions = {
            str(ext).lower().lstrip(".") for ext in extensions
        } or set(DEFAULT_EXTENSIONS)
        self.temp_dir = Path(config.get("temp_dir", "./watcher_data/temp"))
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        cache_path = config.get("hash_cache_path", "./watcher_data/watch_file_cache.db")
        self.cache = FileHashCache(cache_path)
        self.upload_client = UploadClient(
            base_url=str(config.get("upload_api_base_url", "http://127.0.0.1:8000")),
        )
        self._pending: set[str] = set()
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self._observer: Observer | None = None
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """폴더 감시를 시작한다."""
        logger.info("WATCH_START paths=%s recursive=%s", self.watch_paths, self.recursive)
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._process_queue,
            name="FolderWatcherWorker",
            daemon=True,
        )
        self._worker_thread.start()

        observer = Observer()
        handler = FolderWatchHandler(self)
        for watch_path in self.watch_paths:
            watch_path.mkdir(parents=True, exist_ok=True)
            observer.schedule(handler, str(watch_path), recursive=self.recursive)
            logger.info("Watching path=%s", watch_path)
        observer.start()
        self._observer = observer

        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Watcher interrupted")
        finally:
            self.stop()

    def stop(self) -> None:
        """감시를 중지한다."""
        self._stop_event.set()
        self._queue.put(None)
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
            self._worker_thread = None

    def enqueue(self, file_path: Path) -> None:
        """감지된 파일을 처리 큐에 넣는다."""
        if not self._is_supported(file_path):
            return
        try:
            key = str(file_path.resolve())
        except OSError:
            key = str(file_path)
        if key in self._pending:
            return
        self._pending.add(key)
        logger.info("FILE_DETECTED path=%s", file_path)
        self._queue.put(file_path)

    def _process_queue(self) -> None:
        """큐에 쌓인 파일을 순차 처리한다."""
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self.process_file(item)
            finally:
                try:
                    key = str(item.resolve())
                except OSError:
                    key = str(item)
                self._pending.discard(key)

    def process_file(self, file_path: Path) -> bool:
        """
        단일 파일을 처리한다.

        Returns:
            bool: 업로드 성공 여부
        """
        if not self._is_supported(file_path):
            return False

        try:
            if not self._wait_until_stable(file_path):
                logger.warning("FILE_UNSTABLE path=%s", file_path)
                return False

            file_hash = self._compute_sha256_with_retry(file_path)
            if self.cache.has_hash(file_hash):
                logger.info(
                    "UPLOAD_SKIP_DUPLICATE path=%s hash=%s",
                    file_path,
                    file_hash,
                )
                return False

            temp_path = self._copy_with_retry(file_path)
            try:
                logger.info("FILE_COPY source=%s dest=%s", file_path, temp_path)
                self.upload_client.upload(temp_path)
                self.cache.add(file_hash=file_hash, file_path=str(file_path))
                return True
            finally:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)
        except UploadClientError as exc:
            logger.error("UPLOAD_FAILED path=%s error=%s", file_path, exc)
            return False
        except Exception as exc:
            logger.exception("UPLOAD_FAILED path=%s error=%s", file_path, exc)
            return False

    def _is_supported(self, file_path: Path) -> bool:
        extension = file_path.suffix.lower().lstrip(".")
        return bool(extension) and extension in self.extensions

    def _wait_until_stable(self, file_path: Path) -> bool:
        """파일 크기가 2초 간격으로 3회 동일하면 완료로 판단한다."""
        last_size: int | None = None
        stable_count = 0
        for _ in range(STABLE_CHECKS * 5):
            if not file_path.exists():
                return False
            try:
                size = file_path.stat().st_size
            except OSError:
                time.sleep(STABLE_INTERVAL_SECONDS)
                continue

            if last_size is not None and size == last_size and size > 0:
                stable_count += 1
                if stable_count >= STABLE_CHECKS:
                    return True
            else:
                stable_count = 0
            last_size = size
            time.sleep(STABLE_INTERVAL_SECONDS)
        return False

    def _compute_sha256_with_retry(self, file_path: Path) -> str:
        """파일 잠김을 고려해 SHA256을 계산한다."""
        last_error: Exception | None = None
        for attempt in range(1, FILE_LOCK_RETRIES + 1):
            try:
                return self._compute_sha256(file_path)
            except OSError as exc:
                last_error = exc
                logger.warning(
                    "Hash locked path=%s attempt=%s/%s error=%s",
                    file_path,
                    attempt,
                    FILE_LOCK_RETRIES,
                    exc,
                )
                time.sleep(FILE_LOCK_RETRY_DELAY)
        raise OSError(f"Failed to hash file after retries: {file_path}") from last_error

    @staticmethod
    def _compute_sha256(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _copy_with_retry(self, file_path: Path) -> Path:
        """원본을 수정하지 않고 copy2로 임시 파일에 복사한다."""
        temp_path = self.temp_dir / file_path.name
        if temp_path.exists():
            temp_path = (
                self.temp_dir
                / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"
            )

        last_error: Exception | None = None
        for attempt in range(1, FILE_LOCK_RETRIES + 1):
            try:
                shutil.copy2(file_path, temp_path)
                return temp_path
            except OSError as exc:
                last_error = exc
                logger.warning(
                    "Copy locked path=%s attempt=%s/%s error=%s",
                    file_path,
                    attempt,
                    FILE_LOCK_RETRIES,
                    exc,
                )
                time.sleep(FILE_LOCK_RETRY_DELAY)
        raise OSError(f"Failed to copy file after retries: {file_path}") from last_error


def main(argv: list[str] | None = None) -> int:
    """Watcher 진입점."""
    setup_logging()
    args = argv if argv is not None else sys.argv[1:]
    config_path = (
        Path(args[0]) if args else Path(__file__).with_name("watch_config.json")
    )
    config = load_watch_config(config_path)
    FolderWatcher(config).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
