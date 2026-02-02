"""Shared logging setup for the El Paso seismic pipeline."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(name, config, debug=False):
    """Set up console + rotating-file logging.

    Parameters
    ----------
    name : str
        Logger name (also used as the log filename stem).
    config : dict
        Must contain ``log_dir``, ``log_max_bytes``, ``log_backup_count``.
    debug : bool
        If True, set level to DEBUG; otherwise INFO.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # Rotating file
    log_dir = Path(config["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=config["log_max_bytes"],
        backupCount=config["log_backup_count"],
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
