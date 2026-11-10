"""Machinefile text -> instruction dicts.

Dockerfile syntax: backslash continuations, heredocs (<<, <<- with
tab-stripped terminators, quoted delimiters), comment accumulation for
annotations, $VAR expansion against declared ARG/ENV, and the
per-instruction argument forms (ENV pairs, COPY/ADD lists). Instruction
*mapping* to tasks lives in runner_ansible.py.
"""

import re
import shlex
import sys

import yaml

HEREDOC_RE = re.compile(r"<<(-?)(['\"]?)(\w+)\2")


def expand_vars(text: str, known: dict[str, str]) -> str:
    """Expand $VAR/${VAR} only against declared ARG/ENV values.

    Unknown $refs are left intact for the apply-time shell (a Machinefile
    targeting a new machine must not bake in the compiling machine's
    environment). Docker likewise resolves RUN bodies against the image's
    env, not the builder's host env.
    """

    def sub(m: re.Match) -> str:
        key = m.group(1) or m.group(2)
        return known.get(key) or m.group(0) or ""

    return re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, text)


def parse_env(arg: str) -> list[tuple[str, str]]:
    """Support both 'ENV K=V [K2=V2]' and legacy 'ENV K V'."""
    parts = arg.split(None, 1)
    if len(parts) == 1:
        parts = [parts[0], ""]
    if "=" in parts[0]:
        return [
            (m.group(1), m.group(2).strip("\"'"))
            for m in re.finditer(r"(\w+)=(\"[^\"]*\"|'[^']*'|[^\s]+)", arg)
        ]
    return [(parts[0], parts[1].strip().strip("\"'"))]


def copy_add_args(arg: str) -> tuple[list[str], str]:
    """Split COPY/ADD argument into (sources, dest), honoring flags and JSON form."""
    if arg.strip().startswith("["):
        items = yaml.safe_load(arg)
    else:
        items = shlex.split(arg)
    items = [i for i in items if not i.startswith("--")]
    if len(items) < 2:
        sys.exit(f"error: invalid COPY/ADD: {arg!r}")
    return items[:-1], items[-1]


def _collapse(lines: list[str]) -> str:
    return " ".join(l.rstrip("\\").strip() for l in lines)


def parse_lines(text: str) -> list[dict]:
    """Parse Machinefile text into instruction dicts:
    {'instruction', 'value', 'heredoc', 'heredoc_quoted', 'line', 'note'}.

    'note' is the display-only annotation: comment lines directly above
    plus the collapsed instruction line; notes never enter step hashes.
    """
    out: list[dict] = []
    pending: list[str] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        lineno = i + 1
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        joined = [line]
        while line.endswith("\\") and i < n:
            line = lines[i].strip()
            i += 1
            joined.append(line)
        collapsed = _collapse(joined)
        parts = collapsed.split(None, 1)
        instr = parts[0].upper() if parts else ""
        value = parts[1] if len(parts) > 1 else ""
        heredoc = quoted = False
        m = HEREDOC_RE.search(value)
        if m:
            heredoc, quoted = True, bool(m.group(2))
            dash = m.group(1) == "-"
            head = value[: m.start()].rstrip()
            body: list[str] = []
            while i < n:
                raw = lines[i]
                i += 1
                term = raw.strip() if dash else raw.rstrip()
                if term == m.group(3):
                    break
                body.append(raw[1:] if dash and raw.startswith("\t") else raw)
            value = (head + "\n" if head else "") + "\n".join(body)
        out.append({
            "instruction": instr,
            "value": value,
            "heredoc": heredoc,
            "heredoc_quoted": quoted,
            "line": lineno,
            "note": pending + [collapsed],
        })
        pending = []
    return out


def note_name(note: list[str]) -> str:
    """Task name from source annotation: comments then the instruction."""
    comments = [n.lstrip("# ").rstrip() for n in note[:-1]]
    instr_line = note[-1]
    parts = ([f"[{c}]" for c in comments] + [instr_line]) if comments else [instr_line]
    return " ".join(parts)
