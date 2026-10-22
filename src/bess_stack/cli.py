"""Command line: fetch the PSE data, run the study, write tables, README figures and the dashboard.

Usage:
    python -m bess_stack fetch                         # download all PSE series into data/
    python -m bess_stack report                        # results/, docs/img/ and docs/index.html
    python -m bess_stack report --offline --today 2026-10-22
    python -m bess_stack dashboard --out docs          # only the dashboard (used by the scheduled workflow)
    python -m bess_stack report --quick                # no sensitivity runs, two fleet sizes (fast check)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

from bess_stack.pse_api import HISTORY_START, SERIES

log = logging.getLogger("bess_stack")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bess_stack", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--data", type=Path, default=Path("data"), help="cache directory (default: data)")
        sp.add_argument("--today", type=date.fromisoformat, default=None, help="reference date (default: today)")
        sp.add_argument("--offline", action="store_true", help="use the cache only")
        sp.add_argument("-v", "--verbose", action="store_true")

    f = sub.add_parser("fetch", help="download the PSE series into the cache")
    common(f)
    f.add_argument("--series", nargs="+", choices=list(SERIES), default=list(SERIES))
    for name in ("report", "dashboard"):
        r = sub.add_parser(name, help="run the study" if name == "report" else "run the study, write only the dashboard")
        common(r)
        r.add_argument("--out", type=Path, default=Path("docs"), help="dashboard directory (default: docs)")
        r.add_argument("--results", type=Path, default=Path("results"))
        r.add_argument("--workers", type=int, default=None, help="parallel processes (default: CPUs - 1)")
        r.add_argument("--quick", action="store_true", help="skip sensitivity runs, fleets 0 and 1000 MW only")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    today = args.today or date.today()
    from bess_stack.analysis import load_data, run_analysis

    t0 = time.time()
    if args.command == "fetch":
        data = load_data(args.data, today, HISTORY_START, offline=args.offline, series=args.series)
        missing = {k: s.missing for k, s in data.status.items() if s.missing}
        if missing:
            log.warning("months without data: %s", missing)
        return 0

    data = load_data(args.data, today, HISTORY_START, offline=args.offline)
    if data.frames["rce"].empty or data.frames["cap_price"].empty:
        log.error("no energy or capacity prices available")
        return 2
    kw = {"sensitivity": False, "fleets_mw": (0, 1000), "iterations": 3} if args.quick else {}
    result = run_analysis(data, workers=args.workers, **kw)
    from bess_stack.dashboard import write_dashboard

    if args.command == "report":
        from bess_stack.figures import write_figures

        result.write_tables(args.results)
        write_figures(result, args.out / "img")
        log.info("tables in %s, figures in %s", args.results, args.out / "img")
    path = write_dashboard(result, args.out)
    log.info("dashboard %s (%.0f s)", path, time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
