from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Type, TypeVar

import click
import yaml

from latch.types.directory import LatchDir
from latch.types.file import LatchFile
from latch_cli.snakemake.workflow import reindent
from latch_cli.utils import identifier_from_str

from ..serialize_utils import best_effort_display_name
from .utils import (
    JSONValue,
    get_preamble,
    is_primitive_type,
    parse_type,
    parse_value,
    type_repr,
)

T = TypeVar("T")


def parse_config(
    config_path: Path,
    *,
    infer_files: bool = False,
) -> Dict[str, Tuple[Type[T], T]]:
    if not config_path.exists():
        click.secho(
            f"No config file found at {config_path}.",
            fg="red",
        )
        raise click.exceptions.Exit(1)

    if config_path.is_dir():
        click.secho(
            f"Path {config_path} points to a directory.",
            fg="red",
        )
        raise click.exceptions.Exit(1)

    try:
        res: JSONValue = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as e:
        click.secho(
            reindent(
                f"""
                Error loading config from {config_path}:

                {e}
                """,
                0,
            ),
            fg="red",
        )
        raise click.exceptions.Exit(1) from e

    if not isinstance(res, dict):
        # ayush: this case doesn't matter bc a non-dict .yaml file isn't valid snakemake
        return {"snakemake_parameter": (parse_type(res, infer_files=infer_files), res)}

    parsed: Dict[str, Type] = {}
    for k, v in res.items():
        typ = parse_type(v, k, infer_files=infer_files)
        val = parse_value(typ, v)

        parsed[k] = (typ, val)

    return parsed


def generate_file_metadata_str(typ: Type, value: JSONValue, level: int = 0):
    if is_primitive_type(typ):
        return " None,\n"

    if typ in {LatchFile, LatchDir}:
        return reindent(
            f"""\
            SnakemakeFileMetadata(
                path=None, # TODO: add local path here
                config=True,
            ),\n""",
            level,
        )

    if isinstance(value, list):
        metadata: List[str] = [
            generate_file_metadata_str(typ.__args__[0], val, level + 1)
            for i, val in enumerate(value)
        ]
        return reindent(
            f"""\
            [
            __metadata__],\n""",
            level,
        ).replace("__metadata__", "".join(metadata))

    assert is_dataclass(typ)

    metadata: List[str] = []
    for field in fields(typ):
        metadata_str = generate_file_metadata_str(
            field.type, value.__getattribute__(field.name), level
        )

        metadata_str = f"{repr(identifier_from_str(field.name))}: {metadata_str}"
        metadata.append(reindent(metadata_str, level + 1))

    return reindent(
        f"""\
        {{
        __metadata__}},\n""",
        level,
    ).replace("__metadata__", "".join(metadata), level + 1)


# todo(ayush): print informative stuff here ala register
def generate_metadata(
    config_path: Path,
    *,
    skip_confirmation: bool = False,
    generate_defaults: bool = False,
    infer_files: bool = False,
):
    parsed = parse_config(config_path, infer_files=infer_files)

    preambles: List[str] = []
    params: List[str] = []
    file_metadata: List[str] = []

    for k, (typ, default) in parsed.items():
        preambles.append(get_preamble(typ))

        param_str = reindent(
            f"""\
            {repr(identifier_from_str(k))}: SnakemakeParameter(
                display_name={repr(best_effort_display_name(k))},
                type={type_repr(typ)},
            __default__),""",
            0,
        )

        default_str = ""
        if generate_defaults and default is not None:
            default_str = f"    default={repr(default)},\n"

        param_str = param_str.replace("__default__", default_str)

        param_str = reindent(param_str, 1)
        params.append(param_str)

        metadata_str = generate_file_metadata_str(typ, default)
        metadata_str = f"{repr(identifier_from_str(k))}: {metadata_str}"
        file_metadata.append(reindent(metadata_str, 1))

    metadata_root = Path("latch_metadata")
    if metadata_root.is_file():
        if not click.confirm("A file exists at `latch_metadata`. Delete it?"):
            raise click.exceptions.Exit(0)

        metadata_root.unlink()

    metadata_root.mkdir(exist_ok=True)

    metadata_path = metadata_root / Path("__init__.py")
    old_metadata_path = Path("latch_metadata.py")

    if old_metadata_path.exists() and not metadata_path.exists():
        if click.confirm(
            "Found legacy `latch_metadata.py` file in current directory. This is"
            " deprecated and will be ignored in future releases. Move to"
            " `latch_metadata/__init__.py`? (This will not change file contents)"
        ):
            old_metadata_path.rename(metadata_path)
    elif old_metadata_path.exists() and metadata_path.exists():
        click.secho(
            "Warning: Found both `latch_metadata.py` and"
            " `latch_metadata/__init__.py` in current directory."
            " `latch_metadata.py` will be ignored.",
            fg="yellow",
        )

    if not metadata_path.exists() and click.confirm(
        "Could not find an `__init__.py` file in `latch_metadata`. This file"
        "defines the metadata object that configures your interface and "
        "uses parameters imported from `parameters.py`"
        "Generate one?"
    ):
        metadata_path.write_text(
            reindent(
                r"""
                from latch.types.metadata import SnakemakeMetadata, LatchAuthor
                from latch.types.directory import LatchDir

                from .parameters import generated_parameters, file_metadata

                SnakemakeMetadata(
                    output_dir=LatchDir("latch:///your_output_directory"),
                    display_name="Your Workflow Name",
                    author=LatchAuthor(
                        name="Your Name",
                    ),
                    # Add more parameters
                    parameters=generated_parameters,
                    file_metadata=file_metadata,
                )
                """,
                0,
            )
        )

    params_path = metadata_root / Path("parameters.py")
    if (
        params_path.exists()
        and not skip_confirmation
        and not click.confirm(
            "File `latch_metadata/parameters.py` already exists. Overwrite?"
        )
    ):
        raise click.exceptions.Exit(0)

    params_path.write_text(
        reindent(
            r"""
            from dataclasses import dataclass
            import typing

            from latch.types.metadata import SnakemakeParameter, SnakemakeFileParameter, SnakemakeFileMetadata
            from latch.types.file import LatchFile
            from latch.types.directory import LatchDir

            __preambles__

            # Import these into your `__init__.py` file:
            #
            # from .parameters import generated_parameters, file_metadata

            generated_parameters = {
            __params__
            }

            file_metadata = {
            __file_metadata__
            }
            """,
            0,
        )
        .replace("__preambles__", "".join(preambles))
        .replace("__params__", "\n".join(params))
        .replace("__file_metadata__", "".join(file_metadata))
    )
