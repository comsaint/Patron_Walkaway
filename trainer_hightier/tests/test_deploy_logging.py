"""Tests for deploy bundle dual console + file logging bootstrap."""

from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from trainer_hightier.deploy import main as deploy_main


@pytest.fixture
def bundle_rel() -> dict[str, str]:
    """Minimal ``deploy_bundle_paths.json`` shape for logging tests."""
    return {"local_state_dir": "local_state"}


def _stream_handlers(root: logging.Logger) -> list[logging.StreamHandler]:
    return [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]


def _file_handlers(root: logging.Logger) -> list[logging.FileHandler]:
    return [h for h in root.handlers if isinstance(h, logging.FileHandler)]


def _file_handler_for_path(root: logging.Logger, path: Path) -> logging.FileHandler | None:
    """Return the FileHandler writing to *path*, if present."""
    target = str(path.resolve())
    for handler in _file_handlers(root):
        try:
            if str(Path(handler.baseFilename).resolve()) == target:
                return handler
        except OSError:
            continue
    return None


def _handler_order_file_before_stream(root: logging.Logger, log_path: Path) -> bool:
    """Return True when the deploy file handler precedes the stderr stream handler."""
    file_idx: int | None = None
    stream_idx: int | None = None
    for idx, handler in enumerate(root.handlers):
        if isinstance(handler, deploy_main._FlushingFileHandler):
            try:
                if str(Path(handler.baseFilename).resolve()) == str(log_path.resolve()):
                    file_idx = idx
            except OSError:
                continue
        elif isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            if getattr(handler, "stream", None) is sys.stderr:
                stream_idx = idx
    if file_idx is None or stream_idx is None:
        return False
    return file_idx < stream_idx


@pytest.fixture(autouse=True)
def _isolate_root_logging() -> None:
    """Temporarily detach root handlers so deploy bootstrap counts stay stable."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in list(root.handlers):
        root.removeHandler(handler)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_configure_deploy_log_noise_filters_lowers_feast_verbosity() -> None:
    feast_logger = logging.getLogger("feast.infra.registry")
    prior = feast_logger.level
    deploy_main._configure_deploy_log_noise_filters()
    assert feast_logger.level == logging.WARNING
    feast_logger.setLevel(prior)


def test_configure_deploy_log_noise_filters_suppresses_copy_deprecation_message() -> None:
    deploy_main._configure_deploy_log_noise_filters()
    with warnings.catch_warnings(record=True) as caught:
        warnings.warn(
            "The copy keyword is deprecated and will be removed in a future version.",
            FutureWarning,
        )
    assert len(caught) == 0


def test_init_deploy_logging_creates_stream_and_file_handlers(
    tmp_path: Path,
    bundle_rel: dict[str, str],
) -> None:
    """Bootstrap adds stderr and bundle-local file handlers."""
    log_path = deploy_main._init_deploy_logging(
        tmp_path,
        bundle_rel,
        level=logging.INFO,
    )
    root = logging.getLogger()
    assert log_path == tmp_path / "local_state" / "logs" / "deploy_main.log"
    assert log_path.is_file()
    assert any(getattr(h, "stream", None) is sys.stderr for h in _stream_handlers(root))
    assert _file_handler_for_path(root, log_path) is not None
    assert isinstance(_file_handler_for_path(root, log_path), deploy_main._FlushingFileHandler)
    assert _handler_order_file_before_stream(root, log_path)


def test_init_deploy_logging_writes_to_file_without_manual_flush(
    tmp_path: Path,
    bundle_rel: dict[str, str],
) -> None:
    """Flushing file handler persists records without an explicit handler.flush()."""
    log_path = deploy_main._init_deploy_logging(
        tmp_path,
        bundle_rel,
        level=logging.INFO,
    )
    assert log_path is not None
    token = "deploy_logging_flush_marker"
    logging.getLogger("test.deploy.logging.flush").info(token)
    assert token in log_path.read_text(encoding="utf-8")


def test_disable_windows_console_quick_edit_skips_non_windows() -> None:
    with patch.object(deploy_main.sys, "platform", "linux"):
        assert deploy_main._disable_windows_console_quick_edit() is False


def test_disable_windows_console_quick_edit_windows_success() -> None:
    with patch.object(deploy_main.sys, "platform", "win32"):
        with patch("ctypes.windll") as mock_windll:
            mock_kernel32 = mock_windll.kernel32
            mock_kernel32.GetStdHandle.return_value = 7
            mock_kernel32.GetConsoleMode.return_value = 1
            mock_kernel32.SetConsoleMode.return_value = 1
            assert deploy_main._disable_windows_console_quick_edit() is True
            mock_kernel32.SetConsoleMode.assert_called_once()


def test_init_deploy_logging_idempotent_no_duplicate_handlers(
    tmp_path: Path,
    bundle_rel: dict[str, str],
) -> None:
    """Repeated bootstrap must not duplicate handlers."""
    deploy_main._init_deploy_logging(tmp_path, bundle_rel, level=logging.INFO)
    root = logging.getLogger()
    log_path = deploy_main._deploy_log_file_path(tmp_path, bundle_rel)
    first_stream = len(_stream_handlers(root))
    deploy_main._init_deploy_logging(tmp_path, bundle_rel, level=logging.INFO)
    assert len(_stream_handlers(root)) == first_stream
    assert _file_handler_for_path(root, log_path) is not None


def test_init_deploy_logging_writes_to_file(
    tmp_path: Path,
    bundle_rel: dict[str, str],
) -> None:
    """Log records appear in deploy_main.log."""
    log_path = deploy_main._init_deploy_logging(
        tmp_path,
        bundle_rel,
        level=logging.INFO,
    )
    assert log_path is not None
    token = "deploy_logging_test_marker"
    logging.getLogger("test.deploy.logging").info(token)
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert token in log_path.read_text(encoding="utf-8")


def test_init_deploy_logging_file_failure_fail_open(
    tmp_path: Path,
    bundle_rel: dict[str, str],
) -> None:
    """File handler failure keeps console logging and does not raise."""
    blocker = tmp_path / "blocker_file"
    blocker.write_text("x", encoding="utf-8")
    bad_log = blocker / "logs" / "deploy_main.log"
    with patch.object(deploy_main, "_deploy_log_file_path", return_value=bad_log):
        log_path = deploy_main._init_deploy_logging(
            tmp_path,
            bundle_rel,
            level=logging.INFO,
        )
    assert log_path is None
    root = logging.getLogger()
    assert any(getattr(h, "stream", None) is sys.stderr for h in _stream_handlers(root))
    assert _file_handler_for_path(root, bad_log) is None
