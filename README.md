# machinefile

Describe a system setup as a `Machinefile` (Dockerfile syntax), compile it
to an Ansible playbook, apply it locally or over ssh. Docker's layer cache
on a mutable machine, with no custom executor.

## Hello world

Create `Machinefile` (`chmod +x` optional), then `./Machinefile apply`:

```
#!/usr/bin/env -S uvx --from machinefile[ansible] machine
FROM ubuntu
RUN echo hello > /tmp/hello
RUN wc -c < /tmp/hello
```

The only prerequisite is [uv](https://docs.astral.sh/uv/): the shebang lets
uvx fetch the tool ephemerally (no install step); the `[ansible]` extra
adds ansible-core, the executor. Bare `./Machinefile`
prints help; `./Machinefile plan|apply|reset` do the work. Re-apply is a
no-op; re-runs resume at your edit. `./Machinefile reset` forgets this
file's applied layers so the next apply replays everything.

Install the command permanently (optional):

    uv tool install 'machinefile[ansible]'    # or pipx

## Commands

    machine apply Machinefile [user@host]    # local, or ssh (python3 on target)
    machine plan Machinefile [ctx] -o pb.yaml [--arg K=V]...
    machine reset Machinefile [user@host]    # forget this file's applied layers
    machine apply Machinefile -v             # show task output (-vv, -vvv…)
    machine apply Machinefile -- --check     # passthrough to ansible-playbook

apply probes `sudo -n`: without NOPASSWD it prompts for the become password
on a TTY (root/passwordless/CI unaffected; `--no-ask-become-pass` opts out).

Re-apply resumes at your edit: unchanged layers skip, appended layers run
alone, a failed layer retries with its upstream still skipped. That is the
hash chain (`sha256(parent + layer)`, markers in `/var/lib/machinefile/`),
expressed as Ansible `creates:` guards on paired work/marker tasks.

| Instruction           | Becomes                                             |
|-----------------------|-----------------------------------------------------|
| `RUN cmd`             | `shell` + stamp pair (heredocs verbatim, ENV as export prefix) |
| `COPY src dest`       | `copy` with `checksum:` (native idempotency)        |
| `COPY dir dest`       | stamp pair: tree-hash check + `cp -a`               |
| `ADD url dest`        | `get_url` + stamp pair                              |
| `ADD archive dest`    | stamp pair: `tar -xf`                               |
| `ENV K=V`             | export prefix on later layers only; never the host  |
| `USER u`              | `become_user:` on later tasks                       |
| `WORKDIR d`           | directory task + `chdir:` on later tasks            |
| `FROM` `ARG` rest     | comments; ARG expands, `--arg` overrides            |

Output keeps one `# Machinefile:<line>: <full line>` comment and a blank
line per source instruction, so the playbook reads against the original;
comments never enter hashes. Nothing is hoisted or reordered: an apt
install stays below the apt source it needs.

The playbook is a build product: it references context paths and checksums,
so plan and repo travel together, regenerate after `git clone`, gitignore
it. Stamps hash layer text, so pin URLs whose content matters. `USER foo`
presupposes the user exists.

    uv run pytest tests/    # examples/ double as fixtures

Why not bootc: great format, forces an immutable OS. Why not cloud-init:
unreliable reruns outside first boot, Ubuntu-flavored. Here the format is
Docker's, the executor is Ansible's.
