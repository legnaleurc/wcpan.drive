import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)


@dataclass(frozen=True, kw_only=True)
class Setting:
    """One keyword-only parameter of a plugin factory."""

    name: str
    # The annotation without its Annotated metadata; Any when unannotated.
    type: Any
    help: str
    required: bool
    default: Any = None


def get_settings(factory: Callable[..., object]) -> list[Setting]:
    """The settings of *factory*: its keyword-only parameters, in order."""
    return _read(factory)[0]


def check_settings(
    factory: Callable[..., object], settings: Mapping[str, Any]
) -> dict[str, Any]:
    """*settings* checked against the settings of *factory*.

    Keys must name settings, unless the factory also takes ``**kwargs``. Values
    must match the annotations of ``str``, ``int``, ``float``, ``bool``,
    ``None``, unions, lists, and mappings; other annotations are not checked.
    An ``int`` given for a ``float`` becomes a ``float``.

    Raises:
        ValueError: For an unknown, missing, or mistyped setting.
    """
    known, open_ = _read(factory)
    by_name = {_.name: _ for _ in known}
    unknown = sorted(set(settings) - set(by_name))
    if unknown and not open_:
        raise ValueError(f"unknown settings: {', '.join(unknown)}")
    missing = [_.name for _ in known if _.required and _.name not in settings]
    if missing:
        raise ValueError(f"missing settings: {', '.join(missing)}")
    return {
        name: _coerce(value, by_name[name].type, name) if name in by_name else value
        for name, value in settings.items()
    }


def _read(factory: Callable[..., object]) -> tuple[list[Setting], bool]:
    parameters = inspect.signature(factory).parameters.values()
    hints = get_type_hints(inspect.unwrap(factory), include_extras=True)
    settings = [
        _setting(_, hints.get(_.name, Any))
        for _ in parameters
        if _.kind is inspect.Parameter.KEYWORD_ONLY
    ]
    open_ = any(_.kind is inspect.Parameter.VAR_KEYWORD for _ in parameters)
    return settings, open_


def _setting(parameter: inspect.Parameter, hint: Any) -> Setting:
    help_ = ""
    if get_origin(hint) is Annotated:
        help_ = next((_ for _ in hint.__metadata__ if isinstance(_, str)), "")
        hint = get_args(hint)[0]
    required = parameter.default is inspect.Parameter.empty
    return Setting(
        name=parameter.name,
        type=hint,
        help=help_,
        required=required,
        default=None if required else parameter.default,
    )


def _coerce(value: Any, type_: Any, where: str) -> Any:
    origin = get_origin(type_)
    if type_ is Any:
        return value
    if origin is Union or origin is UnionType:
        arms = get_args(type_)
        if value is None and NoneType in arms:
            return None
        others = [_ for _ in arms if _ is not NoneType]
        if len(others) == 1:
            # Report the error inside an optional value.
            return _coerce(value, others[0], where)
        for arm in others:
            try:
                return _coerce(value, arm, where)
            except ValueError:
                pass
    elif type_ is None or type_ is NoneType:
        if value is None:
            return None
    elif type_ is float:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    elif type_ is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif type_ is str or type_ is bool:
        if isinstance(value, type_):
            return value
    elif origin is dict or origin is Mapping:
        if isinstance(value, dict):
            key_type, value_type = get_args(type_) or (Any, Any)
            items = cast(dict[Any, Any], value)
            return {
                _coerce(k, key_type, f"{where} key"): _coerce(
                    v, value_type, f"{where}.{k}"
                )
                for k, v in items.items()
            }
    elif origin is list or origin is Sequence:
        if isinstance(value, list):
            (item_type,) = get_args(type_) or (Any,)
            values = cast(list[Any], value)
            return [
                _coerce(v, item_type, f"{where}[{i}]") for i, v in enumerate(values)
            ]
    else:
        return value
    raise ValueError(f"{where} must be {_describe(type_)}, got {value!r}")


def _describe(type_: Any) -> str:
    if type_ is None or type_ is NoneType:
        return "null"
    if isinstance(type_, type) and get_origin(type_) is None:
        return type_.__name__
    return str(type_).replace("collections.abc.", "").replace("NoneType", "None")
