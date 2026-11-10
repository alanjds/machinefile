"""Docker-like stdout callback: one Machinefile line, then its output.

Activated by `machine apply` (ANSIBLE_CALLBACK_PLUGINS +
ANSIBLE_STDOUT_CALLBACK=machinefile). Format rules:

  - non-noise task start  -> '# Machinefile:<line>: <instruction>'
  - ok                    -> the command's stdout/stderr, if any
  - skipped               -> 'cached' (layer marker already present)
  - failed                -> stdout/stderr + the error message
  - marker/bootstrap tasks and all ansible banners -> silent
  - stats                 -> one summary line

True byte-streaming during a command is not available: ansible
captures module output and delivers it on task completion. Verbose
ansible internals: `machine apply -vvv` (disables this callback).
"""

from ansible.plugins.callback import CallbackBase

BOOTSTRAP = "ensure stamp dir"

# The callback runs inside the ansible-playbook process (usually the
# same venv, where rich is present via our rich-click dep); degrade to
# plain text when it is not (e.g. system ansible on PATH).
try:
    from rich.console import Console
    _console = Console(highlight=False)
except ImportError:
    _console = None


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "machinefile_stdout"  # file name = plugin name for ansible

    def __init__(self):
        super().__init__()
        self._quiet = False

    @staticmethod
    def _out(text):
        print(text, flush=True)

    @staticmethod
    def _label(text):
        # source annotation, styled; markup=False: Machinefile lines may
        # contain literal [brackets]
        if _console is not None:
            _console.print(text, style="bold cyan", markup=False)
        else:
            print(text, flush=True)

    def v2_playbook_on_task_start(self, task, is_conditional):
        name = task.get_name()
        self._quiet = name.startswith("mark:") or name == BOOTSTRAP
        if self._quiet:
            return
        src = task.vars.get("_mf_src") or name
        self._label(f"# {src}")

    def v2_runner_on_ok(self, result):
        if self._quiet:
            return
        r = result._result
        # `creates:` skips arrive as ok (changed=False) with the reason in
        # stdout ('skipped, since <marker> exists'); docker says it better
        # with one word
        if (r.get("skipped")
                or not r.get("changed")
                and (str(r.get("stdout", "")).startswith("skipped, since ")
                     or str(r.get("msg", "")).startswith("Did not run command"))):
            self._out("cached")
            return
        for key in ("stdout", "stderr"):
            text = (r.get(key) or "").strip()
            if text:
                self._out(text)

    def v2_runner_on_skipped(self, result):
        if not self._quiet:
            self._out("cached")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        r = result._result
        for key in ("stdout", "stderr"):
            text = (r.get(key) or "").strip()
            if text:
                self._out(text)
        msg = r.get("msg")
        if msg:
            self._out(f"error: {msg}")

    def v2_playbook_on_stats(self, stats):
        if stats.dark or any(stats.failures.values()):
            self._out("apply FAILED")
        else:
            self._out(f"apply done: {len(stats.changed or {})} host(s) changed")
