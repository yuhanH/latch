import re
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

import gql
from latch_sdk_gql.execute import execute


def _is_valid_url(raw_url: Union[str, Path]) -> bool:
    """A valid URL (as a source or destination of a LatchFile) must:
    * contain a latch or s3 scheme
    * contain an absolute path
    """
    try:
        parsed = urlparse(str(raw_url))
    except ValueError:
        return False
    if parsed.scheme not in ("latch", "s3"):
        return False
    if parsed.path != "" and not parsed.path.startswith("/"):
        return False
    return True


_is_absolute_node_path = re.compile(r"^(latch)?://(?P<node_id>\d+).node(/)?$")

_old_path_expr = re.compile(r"^(?:(?P<account_root>account_root)|(?P<mount>mount))")


def _format_path(path: str) -> str:
    match = _is_absolute_node_path.match(path)

    if match is None:
        return path

    node_id = match.group("node_id")

    data = execute(
        gql.gql("""
        query ldataGetPathQ($id: BigInt!) {
            ldataGetPath(argNodeId: $id)
            ldataOwner(argNodeId: $id)
        }
        """),
        {"id": node_id},
    )

    raw_path: Optional[str] = data["ldataGetPath"]
    if raw_path is None:
        return path

    path_split = raw_path.split("/")

    match = _old_path_expr.match(raw_path)
    if match is None:
        return path

    if match["mount"] is not None:
        bucket = path_split[1]
        key = "/".join(path_split[2:])
        return f"latch://{bucket}.mount/{key}"

    owner: Optional[str] = data["ldataOwner"]
    if owner is None:
        return path

    if match["account_root"] is not None:
        key = "/".join(path_split[2:])
        return f"latch://{owner}.account/{key}"

    return path
