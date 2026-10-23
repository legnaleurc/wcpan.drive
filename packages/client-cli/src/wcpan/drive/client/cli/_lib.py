import sys
from concurrent.futures import Executor, ProcessPoolExecutor
from multiprocessing import get_all_start_methods, get_context
from typing import Any

import yaml


def cout(*values: object) -> None:
    print(*values, file=sys.stdout, flush=True)


def cerr(*values: object) -> None:
    print(*values, file=sys.stderr, flush=True)


def print_as_yaml(data: Any) -> None:
    yaml.safe_dump(
        data,
        stream=sys.stdout,
        allow_unicode=True,
        encoding=sys.stdout.encoding,
        default_flow_style=False,
    )


def create_executor() -> Executor:
    # Python 3.13 still forks by default on Linux, which can deadlock a worker
    # of a multi-threaded process; Windows has no forkserver and spawns.
    # Workers stay in the terminal's process group, so they ignore Ctrl-C and
    # leave it to the main process.
    method = "forkserver" if "forkserver" in get_all_start_methods() else None
    return ProcessPoolExecutor(
        mp_context=get_context(method), initializer=_initialize_worker
    )


def _initialize_worker() -> None:
    from signal import SIG_IGN, SIGINT, signal

    signal(SIGINT, SIG_IGN)
