"""machine: the command-line surface.

Usage:
    machine plan Machinefile [-o playbook.yaml] [--context DIR] [--arg K=V]...
    machine apply Machinefile [user@host] [--context DIR] [--arg K=V]... [-- <ansible args>]

Parsing lives in parser.py, the Ansible backend (task mapping, hash
chain, playbook rendering, execution, stamp removal) in
runner_ansible.py. The playbook is a build product (gitignore it): it
references context file paths and checksums, so plan and repo travel
together.
"""

import sys
from pathlib import Path

import click
import rich_click as rc

from .runner_ansible import (ansible_env, build_ansible_command,
                             compile_tasks, find_ansible,
                             needs_become_password, remove_stamps,
                             render_playbook, run_command)


@rc.group()
def cli():
    """Compile a Machinefile (Dockerfile-syntax) and apply it via Ansible."""


# Shared verb-level flag (each command takes it after its name; click
# cannot inherit group options to positions after the verb).
verbose_opt = rc.option(
    "-v",
    "--verbose",
    count=True,
    help="Verbose: apply forwards to ansible-playbook, "
    "plan/reset report state (repeat: -vv).",
)


def parse_build_args(items: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        out[key] = value
    return out


@cli.command("plan")
@rc.argument("machinefile", type=rc.Path(exists=True, dir_okay=False, path_type=Path))
@rc.argument("context_dir", required=False, default=None,
             type=rc.Path(exists=True, file_okay=False, path_type=Path),
             help="Build context (default: Machinefile's dir).")
@rc.option("-o", "--output", type=rc.Path(path_type=Path), default=None,
           help="Write here instead of stdout.")
@rc.option("-c", "--context", "context_opt",
           type=rc.Path(exists=True, file_okay=False, path_type=Path),
           default=None, help="Build context (same as positional, pick one).")
@rc.option("--arg", "build_args", multiple=True, metavar="K=V",
           help="Override ARG values (repeatable).")
@verbose_opt
def plan_cmd(
    machinefile: Path,
    context_dir: Path | None,
    output: Path | None,
    context_opt: Path | None,
    build_args: tuple[str, ...],
    verbose: int,
):
    """Emit the Ansible playbook for a Machinefile."""
    if context_dir and context_opt:
        sys.exit("error: context given twice")
    context = context_opt or context_dir or machinefile.parent
    tasks, comments = compile_tasks(machinefile, context, parse_build_args(build_args))
    text, _ = render_playbook(tasks, comments, machinefile)
    if verbose:
        rc.echo(f"plan: {len(tasks)} tasks from {machinefile}", err=True)
    if output:
        output.write_text(text)
        rc.echo(f"wrote {output}")
    else:
        sys.stdout.write(text)


@cli.command("apply", context_settings={"ignore_unknown_options": True})
@rc.argument("machinefile", type=rc.Path(exists=True, dir_okay=False, path_type=Path))
@rc.argument("target", required=False, default=None,
             help="user@host or host (default: local machine).")
@rc.option("-c", "--context", type=rc.Path(exists=True, file_okay=False, path_type=Path),
           default=None, help="Build context (default: Machinefile's dir).")
@rc.option("--arg", "build_args", multiple=True, metavar="K=V",
           help="Override ARG values (repeatable).")
@rc.option("--no-ask-become-pass", "ask_become_pass", flag_value=False,
           default=True,
           help="Never probe sudo / prompt for the become password.")
@verbose_opt
@rc.argument("ansible_args", nargs=-1, type=click.UNPROCESSED)
def apply_cmd(
    machinefile: Path,
    target: str | None,
    context: Path | None,
    build_args: tuple[str, ...],
    ask_become_pass: bool,
    verbose: int,
    ansible_args: tuple[str, ...],
):
    """Compile and run the playbook via ansible-playbook.

    Local by default (--connection local); give a target for plain ssh
    transport (python3 required on the remote, nothing else). Extra args
    after `--` pass through to ansible-playbook (e.g. --check, --diff).
    """
    # click binds the first post-`--` token to the optional target even
    # when it is an option (`apply M -- --check`); a host never starts
    # with '-', so hand it to the passthrough instead.
    if target and target.startswith("-"):
        ansible_args = (target, *ansible_args)
        target = None

    # ansible may be missing without the [ansible] extra
    ansible = find_ansible()
    if ansible is None:
        rc.secho(
            "ansible-playbook not found. Install `ansible-core` or the extra 'machinefile[ansible]'",
            err=True,
        )
        sys.exit(1)
    context = context or machinefile.parent
    tasks, comments = compile_tasks(machinefile, context, parse_build_args(build_args))
    text, _ = render_playbook(tasks, comments, machinefile)
    plan_path = machinefile.parent / (machinefile.name + ".ansible.yaml")
    plan_path.write_text(text)
    # become: true is always compiled in; on boxes without NOPASSWD
    # ansible needs to be told to ask (TTY + failed sudo -n probe, see
    # needs_become_password). reset already escalates via sudo.
    ask = ask_become_pass and needs_become_password(list(ansible_args),
                                                    target)
    cmd = build_ansible_command(ansible, plan_path, target, verbose,
                                list(ansible_args), ask)
    rc.echo(f"$ {' '.join(cmd)}", err=True)
    sys.exit(run_command(cmd, ansible_env(verbose)))


@cli.command("reset")
@rc.argument("machinefile", type=rc.Path(exists=True, dir_okay=False, path_type=Path))
@rc.argument(
    "target",
    required=False,
    default=None,
    help="user@host or host (default: local machine).",
)
@rc.option(
    "-c",
    "--context",
    type=rc.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Build context (default: Machinefile's dir).",
)
@rc.option(
    "--arg",
    "build_args",
    multiple=True,
    metavar="K=V",
    help="Override ARG values (must match how it was applied).",
)
@verbose_opt
def reset_cmd(
    machinefile: Path,
    target: str | None,
    context: Path | None,
    build_args: tuple[str, ...],
    verbose: int,
):
    """Forget which layers of THIS Machinefile ran (delete its stamps).

    Recomputes the chain and removes exactly those markers, leaving other
    Machinefiles' stamps on the same host untouched. Next apply re-runs
    every layer. Convergent tasks (COPY) need no reset; they re-converge.
    """
    context = context or machinefile.parent
    tasks, comments = compile_tasks(machinefile, context, parse_build_args(build_args))
    _, stamps = render_playbook(tasks, comments, machinefile)
    if verbose:
        rc.echo(f"reset: {len(stamps)} stamps of {machinefile}", err=True)
    remaining, escalated = remove_stamps(stamps, target)
    if remaining:
        for m in remaining:
            rc.echo(m, err=True)
        rc.secho(f"reset FAILED: {len(remaining)} of {len(stamps)} markers "
                 "remain", err=True)
        sys.exit(1)
    rc.secho(f"reset: {len(stamps)} markers removed"
             + (" (via sudo)" if escalated else ""), err=True)


def main():
    # `#!` exec puts the filename first: `./Machinefile plan` arrives as
    # [<file>, plan, ...extras]. Fixing here by de-swapping before
    # calling Click:
    #   ./Machinefile            -> bare: calls --help instead
    #   ./Machinefile verb ...   -> swap file and verb (verb goes first)
    #   ./Machinefile --help     -> file moves to the end
    # Standard `machine plan Machinefile` works as before.
    argv = sys.argv[1:]
    machinefile = None
    if argv and Path(argv[0]).is_file() and argv[0] not in cli.commands:
        machinefile = argv.pop(0)

    if len(argv) == 0:
        argv = ["--help"]

    if machinefile:
        argv.insert(1, machinefile)

    cli(argv)


if __name__ == "__main__":
    main()
