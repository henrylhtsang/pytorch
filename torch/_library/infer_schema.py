# mypy: allow-untyped-defs
import inspect
import typing
from typing import List, Optional, Sequence, Union  # noqa: F401

import torch  # noqa: F401
from .. import device, dtype, Tensor, types


def infer_schema(prototype_function: typing.Callable, mutates_args=()) -> str:
    """Given a function with type hints, parses a schema.

    We make some assumptions to make our lives easier that correspond to how people
    write custom ops in real life:
    - none of the outputs alias any of the inputs or each other.
    - only the args listed in mutates_args are being mutated.
    - string type annotations "device, dtype, Tensor, types" without library specification
      are assumed to be torch.*. Similarly, string type annotations "Optional, List, Sequence, Union"
      without library specification are assumed to be typing.*.

    Callers (e.g. the custom ops API) are responsible for checking these assumptions.
    """
    sig = inspect.signature(prototype_function)

    def error_fn(what):
        raise ValueError(
            f"infer_schema(func): {what} " f"Got func with signature {sig})"
        )

    def convert_type_string(annotation_type: str):
        try:
            return eval(annotation_type)
        except Exception as e:
            error_fn(
                f"Unsupported type annotation {annotation_type}. It is not a type."
            )

    params = []
    seen_args = set()
    saw_kwarg_only_arg = False
    for idx, (name, param) in enumerate(sig.parameters.items()):
        if not supported_param(param):
            error_fn("We do not support positional-only args, varargs, or varkwargs.")

        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            # The first time we see a kwarg-only arg, add "*" to the schema.
            if not saw_kwarg_only_arg:
                params.append("*")
                saw_kwarg_only_arg = True

        if param.annotation is inspect.Parameter.empty:
            error_fn(f"Parameter {name} must have a type annotation.")

        # The annotation might be converted to a string by annotation,
        # we convert it to the actual type.
        annotation_type = param.annotation
        if type(annotation_type) == str:
            annotation_type = convert_type_string(annotation_type)

        if annotation_type not in SUPPORTED_PARAM_TYPES.keys():
            if annotation_type.__origin__ is tuple:
                list_type = tuple_to_list(annotation_type)
                example_type_str = "\n\n"
                # Only suggest the list type if this type is supported.
                if list_type in SUPPORTED_PARAM_TYPES.keys():
                    example_type_str = f"For example, {list_type}.\n\n"
                error_fn(
                    f"Parameter {name} has unsupported type {param.annotation}. "
                    f"We do not support Tuple inputs in schema. As a workaround, please try to use List instead. "
                    f"{example_type_str}"
                    f"The valid types are: {SUPPORTED_PARAM_TYPES.keys()}."
                )
            else:
                error_fn(
                    f"Parameter {name} has unsupported type {param.annotation}. "
                    f"The valid types are: {SUPPORTED_PARAM_TYPES.keys()}."
                )

        schema_type = SUPPORTED_PARAM_TYPES[annotation_type]
        if name in mutates_args:
            if not schema_type.startswith("Tensor"):
                error_fn(
                    f"Parameter {name} is in mutable_args but only Tensors or collections of Tensors can be mutated"
                )
            schema_type = f"Tensor(a{idx}!){schema_type[len('Tensor'):]}"
        seen_args.add(name)
        if param.default is inspect.Parameter.empty:
            params.append(f"{schema_type} {name}")
        else:
            default_repr = None
            if param.default is None or isinstance(param.default, (int, float, bool)):
                default_repr = str(param.default)
            elif isinstance(param.default, str):
                default_repr = f'"{param.default}"'
            elif isinstance(param.default, torch.dtype):
                dtype_repr = str(param.default)
                torch_dot = "torch."
                assert dtype_repr.startswith(torch_dot)
                default_repr = dtype_repr[len(torch_dot) :]
            else:
                error_fn(
                    f"Parameter {name} has an unsupported default value type {type(param.default)}. "
                    f"Please file an issue on GitHub so we can prioritize this."
                )
            params.append(f"{schema_type} {name}={default_repr}")
    mutates_args_not_seen = set(mutates_args) - seen_args
    if len(mutates_args_not_seen) > 0:
        error_fn(
            f"{mutates_args_not_seen} in mutates_args were not found in "
            f"the custom op's signature. "
            f"mutates_args should contain the names of all args that the "
            f"custom op mutates."
        )
    return_annotation = sig.return_annotation
    if type(return_annotation) == str:
        return_annotation = convert_type_string(return_annotation)
    ret = parse_return(return_annotation, error_fn)
    return f"({', '.join(params)}) -> {ret}"


def derived_types(
    base_type, cpp_type, list_base, optional_base_list, optional_list_base
):
    result = [
        (base_type, cpp_type),
        (typing.Optional[base_type], f"{cpp_type}?"),
    ]

    def derived_seq_types(typ):
        return [
            typing.Sequence[typ],  # type: ignore[valid-type]
            typing.List[typ],  # type: ignore[valid-type]
        ]

    if list_base:
        for seq_typ in derived_seq_types(base_type):
            result.append((seq_typ, f"{cpp_type}[]"))  # type: ignore[valid-type]
    if optional_base_list:
        for seq_typ in derived_seq_types(typing.Optional[base_type]):
            result.append((seq_typ, f"{cpp_type}?[]"))  # type: ignore[valid-type]
    if optional_list_base:
        for seq_typ in derived_seq_types(base_type):  # type: ignore[valid-type]
            result.append((typing.Optional[seq_typ], f"{cpp_type}[]?"))  # type: ignore[valid-type]
    return result


def get_supported_param_types():
    data = [
        # (python type, schema type, type[] variant, type?[] variant, type[]? variant
        (Tensor, "Tensor", True, True, False),
        (int, "SymInt", True, False, True),
        (float, "float", True, False, True),
        (bool, "bool", True, False, True),
        (str, "str", False, False, False),
        (types.Number, "Scalar", True, False, False),
        (dtype, "ScalarType", False, False, False),
        (device, "Device", False, False, False),
    ]
    result = []
    for line in data:
        result.extend(derived_types(*line))
    return dict(result)


SUPPORTED_RETURN_TYPES = {
    Tensor: "Tensor",
    typing.List[Tensor]: "Tensor[]",
    int: "SymInt",
    float: "float",
    bool: "bool",
    types.Number: "Scalar",
}


def parse_return(annotation, error_fn):
    if annotation is None:
        return "()"

    origin = typing.get_origin(annotation)
    if origin is not tuple:
        if annotation not in SUPPORTED_RETURN_TYPES.keys():
            error_fn(
                f"Return has unsupported type {annotation}. "
                f"The valid types are: {SUPPORTED_RETURN_TYPES}."
            )
        return SUPPORTED_RETURN_TYPES[annotation]

    args = typing.get_args(annotation)
    for arg in args:
        if arg not in SUPPORTED_RETURN_TYPES:
            error_fn(
                f"Return has unsupported type {annotation}. "
                f"The valid types are: {SUPPORTED_RETURN_TYPES}."
            )

    return "(" + ", ".join([SUPPORTED_RETURN_TYPES[arg] for arg in args]) + ")"


SUPPORTED_PARAM_TYPES = get_supported_param_types()


def supported_param(param: inspect.Parameter) -> bool:
    return param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def tuple_to_list(tuple_type: typing.Type[typing.Tuple]) -> typing.Type[typing.List]:
    """
    Convert `tuple_type` into a list type with the same type arguments. Assumes that `tuple_type` is typing.Tuple type.
    """
    type_args = getattr(tuple_type, "__args__", None)
    # Account for different python versions, e.g. python 3.8 would give ()
    # but python 3.12 would give None.
    if tuple_type is typing.Tuple or type_args == () or type_args is None:
        # Handle the case of an empty tuple type
        return typing.List
    elif len(type_args) == 1:
        # General case: create a List with the same type arguments
        return typing.List[type_args[0]]  # type: ignore[valid-type]
    elif len(type_args) == 2 and type_args[1] is Ellipsis:  # type: ignore[valid-type]
        return typing.List[type_args[0]]  # type: ignore[valid-type]
    else:
        return typing.List[typing.Union[tuple(type_args)]]  # type: ignore[misc]


def has_tensor_arg(schema: str) -> bool:
    """
    Given a schema string, returns True if the schema has a Tensor arg.
    A Tensor arg is any arg with a type annotation that might involve Tensor.
    """
    inputs = schema.split("->")[0].split("(")[1].split(")")[0].split(",")
    input_types = [input.strip().split(" ")[0] for input in inputs]
    return any("Tensor" in s for s in input_types)


def get_device_arg_id(schema: str) -> Union[int, None]:
    """
    Given a schema string, returns the id of the `device: torch.device` argument.
    If it does not exist, returns None.
    """
    inputs = schema.split("->")[0].split("(")[1].split(")")[0].split(",")

    return next((i for i, s in enumerate(inputs) if "Device device" == s.strip()), None)
