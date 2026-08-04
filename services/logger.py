from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def get_logger(name: str, log_dir: Path, debug: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)
    logger.propagate = False

    log_dir.mkdir(parents=True, exist_ok=True)
    # 原为 2MB×3 = 8MB 硬上限。单条 qq_recv 可带 ≤900 字符 JSON，正常聊天几小时
    # 就能把上限冲满、挤掉早前的痕迹。提到 16MB×5 = 80MB。
    # 需要长期留存的结构化痕迹一律走 core/audit.py 的按天 JSONL 流，不依赖本文件。
    file_handler = RotatingFileHandler(
        log_dir / "yukiko.log",
        maxBytes=16 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    stream_handler.setLevel(level)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger

