from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from textwrap import dedent
from typing import Any, List, Optional, Tuple

import click

import latch.types.metadata as metadata
from latch.types.directory import LatchDir, LatchOutputDir
from latch.types.file import LatchFile
from latch_cli.snakemake.config.utils import get_preamble, type_repr
from latch_cli.snakemake.utils import reindent
from latch_cli.utils import identifier_from_str, urljoins

template = """\
from dataclasses import dataclass
from enum import Enum
import os
import subprocess
import requests
import shutil
from pathlib import Path
import typing
import typing_extensions

from latch.resources.workflow import workflow
from latch.resources.tasks import nextflow_runtime_task, custom_task
from latch.types.file import LatchFile
from latch.types.directory import LatchDir, LatchOutputDir
from latch.ldata.path import LPath
from latch_cli.nextflow.workflow import get_flag
from latch_cli.nextflow.utils import _get_execution_name
from latch_cli.utils import urljoins
from latch.types import metadata
from flytekit.core.annotation import FlyteAnnotation

import latch_metadata

@custom_task(cpu=0.25, memory=0.5, storage_gib=1)
def initialize() -> str:
    token = os.environ.get("FLYTE_INTERNAL_EXECUTION_ID")
    if token is None:
        raise RuntimeError("failed to get execution token")

    headers = {{"Authorization": f"Latch-Execution-Token {{token}}"}}

    print("Provisioning shared storage volume... ", end="")
    resp = requests.post(
        "http://nf-dispatcher-service.flyte.svc.cluster.local/provision-storage",
        headers=headers,
        json={{
            "storage_gib": {storage_gib},
        }}
    )
    resp.raise_for_status()
    print("Done.")

    return resp.json()["name"]


{preambles}

{samplesheet_funs}

@nextflow_runtime_task(cpu={cpu}, memory={memory})
def nextflow_runtime(pvc_name: str, {param_signature}) -> None:
    try:
        shared_dir = Path("/nf-workdir")

{samplesheet_constructors}

        ignore_list = [
            "latch",
            ".latch",
            "nextflow",
            ".nextflow",
            "work",
            "results",
            "miniconda",
            "anaconda3",
            "mambaforge",
        ]

        shutil.copytree(
            Path("/root"),
            shared_dir,
            ignore=lambda src, names: ignore_list,
            ignore_dangling_symlinks=True,
            dirs_exist_ok=True,
        )

        cmd = [
            "/root/nextflow",
            "run",
            str(shared_dir / "{nf_script}"),
            "-work-dir",
            str(shared_dir),
            "-profile",
            "{execution_profile}",
            "-c",
            "latch.config",
{params_to_flags}
        ]

        print("Launching Nextflow Runtime")
        print(' '.join(cmd))
        print(flush=True)

        env = {{
            **os.environ,
            "NXF_HOME": "/root/.nextflow",
            "K8S_STORAGE_CLAIM_NAME": pvc_name,
            "NXF_DISABLE_CHECK_LATEST": "true",
        }}
        subprocess.run(
            cmd,
            env=env,
            check=True,
            cwd=str(shared_dir),
        )
    except subprocess.CalledProcessError:
        print()

        name = _get_execution_name()
        if name is None:
            print("Skipping logs upload, failed to get execution name")
        else:
            remote = LPath(urljoins("{log_dir}", name, "nextflow.log"))
            print(f"Uploading .nextflow.log to {{remote.path}}")
            remote.upload_from(shared_dir / ".nextflow.log")

        raise
    finally:
        token = os.environ.get("FLYTE_INTERNAL_EXECUTION_ID")
        if token is None:
            raise RuntimeError("failed to get execution token")

        headers = {{"Authorization": f"Latch-Execution-Token {{token}}"}}
        resp = requests.post(
            "http://nf-dispatcher-service.flyte.svc.cluster.local/finalize",
            headers=headers,
            json={{
                "pvc_name": pvc_name,
            }}
        )
        if resp.status_code != 200:
            print("Failed to finalize workflow:", resp.status_code)



@workflow(metadata._nextflow_metadata)
def {workflow_func_name}({param_signature_with_defaults}) -> None:
    pvc_name: str = initialize()
    nextflow_runtime(pvc_name=pvc_name, {param_args})

"""


def _get_flags_for_dataclass(name: str, val: Any) -> List[str]:
    assert is_dataclass(val)

    flags = []
    for f in fields(val):
        flags.extend(get_flag(f"{name}.{f.name}", getattr(val, f.name)))

    return flags


def get_flag(name: str, val: Any) -> List[str]:
    flag = f"--{name}"

    if val is None:
        return []
    elif isinstance(val, bool):
        return [flag] if val else []
    elif isinstance(val, LatchFile) or isinstance(val, LatchDir):
        if val.remote_path is not None:
            return [flag, val.remote_path]

        return [flag, str(val.path)]
    elif is_dataclass(val):
        return _get_flags_for_dataclass(name, val)
    elif isinstance(val, Enum):
        return [flag, getattr(val, "value")]
    else:
        return [flag, str(val)]


def generate_nextflow_config(pkg_root: Path):
    config_path = Path(pkg_root) / "latch.config"
    config_path.write_text(dedent("""\
        process {
            executor = 'k8s'
        }

        aws {
            client {
                anonymous = true
            }
        }
        """))

    click.secho(f"Nextflow Latch config written to {config_path}", fg="green")


def generate_nextflow_workflow(
    pkg_root: Path,
    nf_script: Path,
    dest: Path,
    *,
    execution_profile: Optional[str] = None,
):
    generate_nextflow_config(pkg_root)

    assert metadata._nextflow_metadata is not None

    wf_name = metadata._nextflow_metadata.name
    assert wf_name is not None

    parameters = metadata._nextflow_metadata.parameters
    resources = metadata._nextflow_metadata.runtime_resources

    flags = []
    defaults: List[Tuple[str, str]] = []
    no_defaults: List[str] = []
    preambles: set[str] = set()
    samplesheet_funs: List[str] = []
    samplesheet_constructors: List[str] = []
    for param_name, param in parameters.items():
        sig = f"{param_name}: {type_repr(param.type)}"
        if param.default is not None:
            if isinstance(param.default, Enum):
                defaults.append((sig, param.default))
            elif param.type in {LatchDir, LatchFile}:
                defaults.append((
                    sig,
                    f"{param.type.__name__}('{param.default._raw_remote_path}')",
                ))
            elif param.type is LatchOutputDir:
                defaults.append((
                    sig,
                    f"LatchOutputDir('{param.default._raw_remote_path}')",
                ))
            else:
                defaults.append((sig, repr(param.default)))
        else:
            no_defaults.append(sig)

        if param.samplesheet:
            samplesheet_funs.append(
                reindent(
                    f"""
                    {param_name}_construct_samplesheet = metadata._nextflow_metadata.parameters[{repr(param_name)}].samplesheet_constructor
                    """,
                    0,
                ),
            )

            samplesheet_constructors.append(
                reindent(
                    f"{param_name}_samplesheet ="
                    f" {param_name}_construct_samplesheet({param_name})",
                    2,
                ),
            )

            flags.append(
                reindent(
                    f"*get_flag({repr(param_name)}, {param_name}_samplesheet)",
                    2,
                )
            )
        else:
            flags.append(reindent(f"*get_flag({repr(param_name)}, {param_name})", 4))

        preamble = get_preamble(param.type)
        if len(preamble) > 0 and preamble not in preambles:
            preambles.add(preamble)

    if metadata._nextflow_metadata.log_dir is None:
        log_dir = "latch:///nextflow_logs"
    else:
        log_dir = metadata._nextflow_metadata.log_dir._raw_remote_path
    log_dir = urljoins(log_dir, wf_name)

    entrypoint = template.format(
        workflow_func_name=identifier_from_str(wf_name),
        nf_script=nf_script.resolve().relative_to(pkg_root.resolve()),
        param_signature_with_defaults=", ".join(
            no_defaults + [f"{name} = {val}" for name, val in defaults]
        ),
        param_signature=", ".join(no_defaults + [name for name, _ in defaults]),
        param_args=", ".join(
            f"{param_name}={param_name}" for param_name in parameters.keys()
        ),
        params_to_flags=",\n".join(flags),
        execution_profile=(
            execution_profile if execution_profile is not None else "standard"
        ),
        preambles="\n\n".join(list(preambles)),
        samplesheet_funs="\n".join(samplesheet_funs),
        samplesheet_constructors="\n".join(samplesheet_constructors),
        cpu=resources.cpus,
        memory=resources.memory,
        storage_gib=resources.storage_gib,
        log_dir=log_dir,
    )

    dest.write_text(entrypoint)
    click.secho(f"Nextflow workflow written to {dest}", fg="green")
