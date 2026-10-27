import sys
from concurrent.futures import Executor, ProcessPoolExecutor
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
    from multiprocessing import get_start_method

    if get_start_method() == "spawn":
        return ProcessPoolExecutor(initializer=_initialize_worker)
    else:
        return ProcessPoolExecutor()


def _initialize_worker() -> None:
    from signal import SIG_IGN, SIGINT, signal

    signal(SIGINT, SIG_IGN)
