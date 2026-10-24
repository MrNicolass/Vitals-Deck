from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from turzx_monitor import __version__
from turzx_monitor.app import MonitorApp, setup_logging
from turzx_monitor.config import ConfigError, load_config
from turzx_monitor.preview import render_preview

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turzx-monitor",
        description="Monitor de sistema para a tela TURZX 3.5\".",
    )
    parser.add_argument("--config", default="config.yaml", help="caminho do config.yaml")
    parser.add_argument(
        "--preview",
        metavar="PNG",
        help="renderiza um frame offline no arquivo informado e encerra",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"erro de configuração: {exc}", file=sys.stderr)
        return 2

    setup_logging(config)

    if args.preview:
        try:
            destination = render_preview(config, args.preview)
        except Exception as exc:
            logger.error("falha ao gerar preview: %s", exc)
            return 1
        logger.info("preview salvo em %s", destination)
        return 0

    MonitorApp(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
