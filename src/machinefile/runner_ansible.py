"""The Ansible backend: Machinefile instructions -> tasks -> playbook -> execution.

Mapping (Dockerfile instruction -> playbook task):
    RUN  <cmd>      -> ansible.builtin.shell (stamp pair); heredocs pass
                       verbatim in a YAML block scalar; env + chdir per task
    COPY file       -> ansible.builtin.copy with checksum: (drift-aware)
    COPY dir        -> stamped shell: tree sha256 check + cp -a
    ADD  url dest   -> ansible.builtin.get_url (stamp pair)
    ADD  archive    -> stamped shell: tar -xf into dest
    ENV  K=V        -> NOT persisted to the host: attached as `environment:`
                       to subsequent tasks (Docker layer scope)
    USER u          -> become_user on subsequent tasks
    WORKDIR d       -> file: state=directory; chdir on subsequent tasks
    ARG             -> compile-time value for $ expansion; commented
    FROM / others   -> comments (informational)

Execution, privilege, transport are Ansible's job:
  - local:  machine apply Mf            (ansible-playbook --connection local)
  - remote: machine apply Mf user@host  (plain ssh, python3 on target)
  - USER u  -> become_user: u (no sudo-in-bash quoting anywhere)

Docker layer semantics, enforced by Ansible rather than hand-rolled bash:

  - every instruction becomes its own task, never reordered or merged;
  - imperative RUN layers run at most once: the layer is paired with
    `creates: /var/lib/machinefile/<chain-hash>` and a following
    marker task (marker only after success);
  - convergent instructions (file COPY, get_url, directory) have native
    Ansible idempotency and need no marker;
  - the hash chain mirrors Docker: editing a layer (or its COPY'd bytes,
    carried as `checksum:`) changes its hash and every downstream hash,
    so re-applying resumes exactly at the edit point, and appending
    layers never invalidates the past.

Reset for a full re-apply: rm -rf /var/lib/machinefile
"""

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from .parser import copy_add_args, expand_vars, note_name, parse_env, parse_lines

STAMP_DIR = os.environ.get("MACHINEFILE_STAMP_DIR", "/var/lib/machinefile")

TARBALL_RE = re.compile(r"\.(tar|tar\.gz|tgz|tar\.bz2|tar\.xz)$", re.IGNORECASE)


def env_assign(k: str, v: str) -> str:
    """Render K=V for an apply-time `export`, preserving literal $refs.

    Ansible's `environment:` sets literal strings (no shell re-expansion),
    so ENV values referencing runtime vars (PATH=...:$PATH) must be carried
    into the shell command itself. shlex.quote would single-quote and freeze
    $refs; use double quotes with the few active characters escaped. Values
    come from the (trusted) Machinefile; a hostile Machinefile is arbitrary
    code anyway via RUN.
    """
    if not v or any(c.isspace() or c in ';&|<>()"\\`' for c in v):
        v = v.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
        return f'{k}="{v}"'
    return f"{k}={v}"


def file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree_sha256(d: Path) -> str:
    """sha256 over file contents in sorted relative-path order."""
    h = hashlib.sha256()
    for p in sorted(d.rglob("*"), key=lambda p: str(p.relative_to(d))):
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def compile_tasks(machinefile: Path, context: Path,
                  build_args: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """Return (ordered playbook task dicts, header comments).

    Task dicts carry the final module args plus stamp metadata; a task with
    stamp=True gets `creates:` + a paired touch marker at render time. The
    hash chain is computed over each layer's serialized module args, so any
    edit (instruction text or COPY'd bytes via checksum) cascades
    downstream exactly like Docker.
    """
    structure = parse_lines(machinefile.read_text())

    tasks: list[dict] = []
    comments: list[str] = []
    known: dict[str, str] = dict(build_args or {})  # --arg seeds first
    env: dict[str, str] = {}
    user = ""
    workdir = ""

    def add(module: str, args: dict, entry: dict, stamp: bool = False):
        if env and module == "ansible.builtin.shell":
            # Layer env via in-command exports (see env_assign): the values
            # must survive shell expansion at apply time, and COPY-style
            # modules carry no shell at all (matches Docker).
            prefix = " && ".join(f"export {env_assign(k, v)}" for k, v in env.items())
            args = {**args, "cmd": prefix + "\n" + args["cmd"]}
        task: dict = {"name": note_name(entry["note"])}
        task["_src"] = (entry["line"], entry["note"][-1])
        if stamp:
            task["_stamp"] = True
        task[module] = args
        if user and user != "root":
            task["become_user"] = user
        if module in ("ansible.builtin.shell", "ansible.builtin.command") and workdir:
            task["args"] = {"chdir": workdir}
        tasks.append(task)

    def emit_copy(src: str, dest: str, entry: dict):
        f = context / src
        if not f.exists():
            sys.exit(f"error: source not found in context: {f}")
        dest = expand_vars(dest, known)
        if dest.endswith("/"):
            dest = dest + f.name
        if f.is_dir():
            # No native recursive-copy module: keep a stamped shell with a
            # tree-hash guard (same definition as tree_sha256: sorted
            # relative paths, concatenated contents).
            h = tree_sha256(f)
            src_q, dest_q = shlex.quote(str(f)), shlex.quote(dest)
            add("ansible.builtin.shell", {
                "cmd": (
                    f'test "$(cd {src_q} && find . -type f -print0 | LC_ALL=C sort -z '
                    f"| xargs -0 -r cat | sha256sum | cut -d' ' -f1)\" = {h} "
                    f"&& install -d {dest_q} && cp -a {src_q}/. {dest_q}"
                )
            }, entry, stamp=True)
            return
        # Native convergent copy: checksum binds the layer hash to the
        # source bytes (content edit = Docker COPY cache invalidation),
        # and remote transfers verify integrity with it.
        mode = f"{f.stat().st_mode & 0o777:04o}"
        add("ansible.builtin.copy", {
            "src": str(f),
            "dest": dest,
            "mode": mode,
            "checksum": f"sha256:{file_sha256(f)}",
        }, entry)

    for entry in structure:
        instr, arg = entry["instruction"], entry["value"]
        if instr == "FROM":
            comments.append(f"base image intent: FROM {arg}")
        elif instr == "ARG":
            key, _, default = arg.partition("=")
            key = key.strip()
            if key not in (build_args or {}):
                known[key] = expand_vars(default.strip().strip("\"'"), known)
            comments.append(f"ARG {arg}")
        elif instr == "RUN":
            cmd = arg if entry["heredoc_quoted"] else expand_vars(arg, known)
            add("ansible.builtin.shell", {"cmd": cmd}, entry, stamp=True)
        elif instr == "COPY":
            srcs, dest = copy_add_args(arg)
            for src in srcs:
                emit_copy(src, dest, entry)
        elif instr == "ADD":
            srcs, dest = copy_add_args(arg)
            for src in srcs:
                src_x = expand_vars(src, known)
                dest_x = expand_vars(dest, known)
                if src_x.startswith(("http://", "https://")):
                    add("ansible.builtin.get_url", {"url": src_x, "dest": dest_x},
                        entry, stamp=True)
                elif TARBALL_RE.search(src_x):
                    src_q, dest_q = shlex.quote(str(context / src_x)), shlex.quote(dest_x)
                    add("ansible.builtin.shell", {
                        "cmd": f"install -d {dest_q} && tar -xf {src_q} -C {dest_q}"
                    }, entry, stamp=True)
                else:
                    emit_copy(src, dest, entry)
        elif instr == "ENV":
            # Layer-scoped (Docker semantics): attached as task
            # `environment:` for subsequent layers, never persisted to the
            # host. Want a permanent env var? Write profile.d via COPY/RUN.
            for key, value in parse_env(arg):
                value = expand_vars(value, known)
                known[key] = value
                env[key] = value
        elif instr == "USER":
            user = expand_vars(arg, known).strip()
        elif instr == "WORKDIR":
            workdir = arg.strip()
            add("ansible.builtin.file",
                {"path": workdir, "state": "directory"}, entry)
        else:
            comments.append(f"ignored: {instr} {arg}")

    return tasks, comments


def render_playbook(
    tasks: list[dict], comments: list[str], machinefile: Path | None = None
) -> tuple[str, list[str]]:
    """Serialize tasks into a playbook, resolving stamp pairs.

    Stamped layers become two tasks: the work (guarded by `creates:` on the
    chain-hash marker) and a `touch` of that marker. A failed layer aborts
    before its touch, so it retries on the next apply; an unchanged plan
    leaves every marker in place and Ansible skips every layer.
    """

    markers: list[str] = []

    def flush(out: list[tuple], task: dict):
        src = task.pop("_src", None)
        stamp = task.pop("_stamp", None)
        args = next(v for k, v in list(task.items()) if k not in
                    ("name", "environment", "become_user"))
        module = next(k for k in task if k.startswith("ansible.builtin."))
        layer = yaml.safe_dump({module: args}, sort_keys=True, width=10**6)
        nonlocal chain
        chain = hashlib.sha256(f"{chain}\n{layer}".encode()).hexdigest()[:16]
        marker = f"{STAMP_DIR}/{chain}"
        if stamp:
            markers.append(marker)
            task.setdefault("args", {})["creates"] = marker
            out.append((src, task))
            # Marker as command+creates: skipped when present, runs `touch`
            # once when absent. (The file module has no `creates`, and its
            # state:touch re-reports changed on every pass.)
            out.append((src, {"name": f"mark: {task['name']}",
                              "ansible.builtin.command": {"cmd": f"touch {marker}"},
                              "args": {"creates": marker}}))
        else:
            out.append((src, task))

    chain = "machinefile-root"
    resolved: list[tuple] = []
    for task in tasks:
        flush(resolved, task)
    if any("creates" in str(t.get("args", "")) for _, t in resolved):
        # convergent bootstrap (never hashed): touch needs the dir
        resolved.insert(0, (None, {"name": "ensure stamp dir",
                                   "ansible.builtin.file": {"path": STAMP_DIR,
                                                            "state": "directory"}}))

    header = ["# Generated by machinefile. Build product: do not commit;"]
    if machinefile:
        header[0] = f"# Generated by machinefile from {machinefile}. Build product: do not commit;"
    for c in comments:
        header.append(f"# {c}")
    return _emit(resolved, header), markers


def _emit(items: list[tuple], header: list[str]) -> str:
    """Playbook YAML written task-by-task (safe_dump cannot interleave
    comments/blank lines). Tasks from one Machinefile line form a group:
    one '# Machinefile:<line>: <full line>' comment above the group, one
    blank line after it. Comments never enter the hash chain. Each task
    also carries _mf_src as a variable: the machinefile_stdout callback
    reads it to label output (vars are schema-free, arbitrary task keys
    are not; injection happens post-hash, so the chain is unaffected)."""
    lines = header + ["- hosts: all", "  gather_facts: false",
                      "  become: true", "  tasks:"]
    prev = object()
    for src, task in items:
        if src != prev:
            if prev is not object() and src is not None:
                lines.append("")  # gap after the previous line's group
            if src is not None:
                lines.append(f"  # Machinefile:{src[0]}: {src[1]}")
        if src is not None:
            task = {**task, "vars": {"_mf_src": f"Machinefile:{src[0]}: {src[1]}"}}
        body = yaml.safe_dump(task, default_flow_style=False,
                              sort_keys=False, width=10**6, allow_unicode=True)
        body_lines = body.rstrip().splitlines()
        lines.append("  - " + body_lines[0])
        lines.extend("    " + l for l in body_lines[1:])
        prev = src
    return "\n".join(lines) + "\n"


def find_ansible() -> str | None:
    """Locate ansible-playbook: PATH first, then our own venv's bin
    (uv tool install puts the entry point there, not on PATH)."""
    return shutil.which("ansible-playbook") or shutil.which(
        "ansible-playbook", path=os.path.dirname(sys.executable))


def ansible_env(verbose: int) -> dict[str, str]:
    """Environment for ansible-playbook: our docker-like stdout callback,
    unless -vvv asks for raw ansible internals."""
    env = dict(os.environ)
    if verbose < 3:
        env["ANSIBLE_CALLBACK_PLUGINS"] = str(Path(__file__).parent / "callbacks")
        env["ANSIBLE_STDOUT_CALLBACK"] = "machinefile_stdout"
    env.setdefault("ANSIBLE_NOCOWS", "1")
    return env


def run_command(cmd: list[str], env: dict[str, str] | None = None) -> int:
    """Execute a built command (the CLI owns messaging, we own processes)."""
    return subprocess.call(cmd, env=env)


def build_ansible_command(ansible: str, plan_path: Path, target: str | None,
                          verbose: int, ansible_args: list[str]) -> list[str]:
    cmd = [ansible, str(plan_path)]
    if target:
        cmd += ["-i", f"{target},"]
    else:
        cmd += ["-i", "localhost,", "--connection", "local"]
    cmd += ["-" + "v" * verbose] if verbose else []
    cmd += ansible_args
    return cmd


def remove_stamps(stamps: list[str], target: str | None) -> tuple[list[str], bool]:
    """Delete marker files; return (still-present markers, escalated).

    Truth is decided by re-checking existence, not by exit codes: the
    plain pass is expected to fail on /var/lib (markers are root-owned
    after apply's `become`), so it is a quiet probe and sudo runs only
    when it fails (user-owned sandbox dirs stay sudo-free).
    """
    if not stamps:
        return [], False
    quoted = " ".join(shlex.quote(m) for m in stamps)
    # brace the chain: `{ rm && rm; } 2>/dev/null` silences every rm (a
    # bare suffix redirect would cover only the last one)
    rm = "{ " + " && ".join(f"rm -f {p}" for p in stamps) + "; }"
    check = f'for f in {quoted}; do [ -e "$f" ] && echo "$f"; done; true'
    escalated = False
    if target:
        if subprocess.call(["ssh", target, f"{rm} 2>/dev/null"]):
            escalated = True
            subprocess.call(["ssh", "-t", target,
                             f"sudo sh -c {shlex.quote(rm)}"])
        out = subprocess.run(["ssh", target, check],
                             capture_output=True, text=True)
    else:
        if subprocess.call(["bash", "-c", f"{rm} 2>/dev/null"]):
            escalated = True
            if os.geteuid() != 0:
                subprocess.call(["bash", "-c",
                                 f"sudo sh -c {shlex.quote(rm)}"])
        out = subprocess.run(["bash", "-c", check],
                             capture_output=True, text=True)
    return out.stdout.split(), escalated
