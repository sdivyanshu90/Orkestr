"""Policy engine.

Every tool invocation passes through :meth:`PolicyEngine.authorize` before it
runs.  The engine answers three questions:

1. **Capability** -- does the caller's capability set permit the required
   capabilities of this tool?
2. **Scope**      -- is the operation confined to the paths the task is
   allowed to touch?
3. **Authority**  -- does the operation require explicit human approval
   (network access, commits, pushes, deletions, protected paths)?

Only after all three answers are "yes" may the tool run.  The decision and
its reasoning are recorded in the audit log, including denials, because a
denial is exactly the event an operator needs to see.

The engine never reads model output to decide what is allowed.  It reads the
*declared intent* of a tool call and the *static* policy configuration.

One property deserves emphasis: **the tool surface is not the security
boundary, the policy is.**  A worker that wants to delete a file, push a
branch or fetch a URL will try the cheapest route available, and that route
is usually not the one the policy was written for.  So the engine looks at
the *effect* a call would have rather than at the tool that was named:

* a path argument is checked against the scope and the protected material;
* a command is scanned, so ``rm``, ``sed -i``, a redirection, ``git push``
  hidden inside ``sh -c`` and a file write smuggled through ``python3 -c``
  are all judged by what they do, not by the tool they arrived in.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..config import ApprovalConfig, SandboxConfig
from ..errors import PolicyError
from .approval import ApprovalBinding
from .capabilities import Capability, CapabilitySet
from .injection import redact_secrets


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str = ""
    required_approval: bool = False
    action: str = ""
    capabilities: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "required_approval": self.required_approval,
            "action": self.action,
            "capabilities": list(self.capabilities),
        }


@dataclass
class ToolRule:
    """The capabilities a tool requires and whether it needs human approval."""

    name: str
    required_capabilities: tuple[str, ...]
    needs_approval: bool = False
    approval_action: str = ""
    path_argument: str | None = None
    description: str = ""


DEFAULT_TOOL_RULES: tuple[ToolRule, ...] = (
    ToolRule(
        "read_file",
        (Capability.FS_READ,),
        path_argument="path",
        description="Read a file from the task scope.",
    ),
    ToolRule(
        "list_dir",
        (Capability.FS_READ,),
        path_argument="path",
        description="List a directory inside the task scope.",
    ),
    ToolRule(
        "write_file",
        (Capability.FS_WRITE,),
        path_argument="path",
        description="Create or overwrite a file inside the task scope.",
    ),
    ToolRule(
        "run_command",
        (Capability.EXEC_RUN,),
        description="Execute an authorised command in the sandbox.",
    ),
    ToolRule(
        "git_diff",
        (Capability.EXEC_GIT,),
        description="Show the working tree diff.",
    ),
    ToolRule(
        "git_status",
        (Capability.EXEC_GIT,),
        description="Show the working tree status.",
    ),
    ToolRule(
        "commit_changes",
        (Capability.GIT_COMMIT,),
        needs_approval=True,
        approval_action="git.commit",
        description="Create a git commit (requires human approval).",
    ),
    ToolRule(
        "push_changes",
        (Capability.GIT_PUSH,),
        needs_approval=True,
        approval_action="git.push",
        description="Push to a remote (requires human approval).",
    ),
    ToolRule(
        "http_request",
        (Capability.NET_HTTP,),
        needs_approval=True,
        approval_action="net.http",
        description="Perform an outbound HTTP request (requires human approval).",
    ),
    ToolRule(
        "delete_path",
        (Capability.FS_DELETE,),
        needs_approval=True,
        approval_action="fs.delete",
        path_argument="path",
        description="Delete a path (requires human approval).",
    ),
    ToolRule(
        "emit_artifact",
        (Capability.ARTIFACT_WRITE,),
        description="Publish a structured artifact to the artifact store.",
    ),
    ToolRule(
        "request_approval",
        (Capability.APPROVAL_REQUEST,),
        description="Ask a human to authorise a protected action.",
    ),
)

#: Files that decide how the checks are run.
#:
#: They are not the worker's to change: a worker that rewrites
#: ``pytest.ini`` or ``conftest.py`` can make a hidden check disappear
#: without touching a single line of the code the check is about.  They
#: are therefore outside the worker's write authority, exactly like the
#: protected verification material itself.
EVALUATOR_CONFIG_PATHS: tuple[str, ...] = (
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
    "conftest.py",
    "**/conftest.py",
)


# --------------------------------------------------------------------------
# command scanning
# --------------------------------------------------------------------------

#: Shells whose script text has to be read to know what the command does.
_SHELLS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})

#: Programs that remove something.
_DELETE_PROGRAMS: frozenset[str] = frozenset({"rm", "rmdir", "unlink"})

#: Programs that leave the host.
_NETWORK_PROGRAMS: frozenset[str] = frozenset({"curl", "wget"})

#: Programs that copy or move content, and therefore write a destination.
_COPY_PROGRAMS: frozenset[str] = frozenset({"cp", "mv", "install"})

#: Programs that write to a path they are given.
_WRITE_PROGRAMS: frozenset[str] = frozenset(
    {
        "tee",
        "touch",
        "truncate",
        "shred",
        "chmod",
        "chown",
        "mkdir",
        "ln",
        "dd",
    }
)

#: Git subcommands that write to the working tree.
_GIT_WRITE_SUBCOMMANDS: frozenset[str] = frozenset(
    {"checkout", "restore", "reset", "clean", "apply", "rm", "mv", "add"}
)

#: Git subcommands that are protected effects in their own right.
_GIT_APPROVAL_ACTIONS: dict[str, str] = {
    "push": "git.push",
    "commit": "git.commit",
}

_SHELL_SPLIT = re.compile(r"(?:;|\|\||&&|\||\n)")
_SHELL_TOKEN = re.compile(r"(\"[^\"]*\"|'[^']*'|\S+)")
_REDIRECTION = re.compile(r"^(?:[0-9]*>>?|&>)$")
#: The quoted parts of a token.
#:
#: A path that reaches the shell inside quotes -- ``open('a/b.py','w')`` --
#: is still a path, and a scanner that only looked at whole tokens would
#: miss exactly the way a determined worker smuggles one past.
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")

#: The calls that write a file, used to recognise a write that is spelled
#: inside a quoted program rather than as a command of its own.
_WRITE_MARKERS: tuple[str, ...] = (
    "open(",
    ".write",
    "write_text",
    "write_bytes",
    "remove(",
    "unlink(",
    "rmtree",
    "truncate",
    "shutil.",
    "rename(",
    "replace(",
)


@dataclass(frozen=True)
class CommandScan:
    """What a command would do, as far as static analysis can tell.

    ``write_paths`` are the paths the command would modify; they are what
    the scope and the protected material are checked against.
    ``referenced_paths`` are every path the command mentions, and they are
    what the protected material is checked against, because material that
    is externally owned must not be *read* either.

    The scan is a conservative approximation, not a parser: it recognises
    the shapes that matter (a redirection, a mutating program, a git
    subcommand, a protected action hidden inside a shell script) and treats
    everything else as unknown.  The structural controls -- the worktree
    boundary, the executable allow-list, the staged protected material --
    are what make the unknown cases safe.
    """

    argv: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    referenced_paths: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    shell: bool = False

    @property
    def mutating(self) -> bool:
        return bool(self.write_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "write_paths": list(self.write_paths),
            "referenced_paths": list(self.referenced_paths),
            "actions": list(self.actions),
            "shell": self.shell,
        }


def _as_argv(command: Any) -> tuple[str, ...]:
    if isinstance(command, str):
        return tuple(part for part in command.split() if part)
    if isinstance(command, (list, tuple)):
        return tuple(str(part) for part in command)
    return ()


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _looks_like_path(token: str) -> bool:
    """Whether a token looks like a filesystem path.

    URLs are excluded: ``https://example.com/x`` contains a slash but is
    not a path, and treating it as one would make every network call look
    like a file write.
    """
    if not token or token.startswith("-"):
        return False
    if "://" in token:
        return False
    if token.startswith("~"):
        return True
    return "/" in token or "\\" in token


def _shell_script(argv: tuple[str, ...]) -> str | None:
    """The script text a shell invocation would run, if it is inline."""
    if len(argv) >= 3 and argv[1] in ("-c", "-lc", "-ec", "-c"):
        return argv[2]
    for part in argv[1:]:
        if not part.startswith("-"):
            # ``sh script.sh``: the script is a file the worker controls,
            # and reading it would be reading the worker's own text.
            return None
    return None


def _split_shell(script: str) -> list[list[str]]:
    """Split a shell script into the individual commands it runs."""
    commands: list[list[str]] = []
    for chunk in _SHELL_SPLIT.split(script):
        tokens = [_strip_quotes(t) for t in _SHELL_TOKEN.findall(chunk)]
        tokens = [t for t in tokens if t]
        if tokens:
            commands.append(tokens)
    return commands


def _redirection_targets(tokens: list[str]) -> list[str]:
    """The paths a command redirects its output into."""
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if not _REDIRECTION.match(token):
            continue
        if index + 1 < len(tokens):
            candidate = tokens[index + 1]
            if _looks_like_path(candidate) or "/" not in candidate:
                targets.append(candidate)
    return targets


def _non_flag(tokens: list[str]) -> list[str]:
    return [t for t in tokens if not t.startswith("-")]


def scan_command(command: Any) -> CommandScan:
    """Analyse a command and report what it would do.

    The analysis is deliberately shallow: it recognises the commands and
    redirections that change the filesystem or perform a protected
    effect, and it looks inside a shell script rather than trusting the
    shell to be the only thing that runs.
    """
    argv = _as_argv(command)
    if not argv:
        return CommandScan()
    program = posixpath.basename(argv[0])
    if program in _SHELLS:
        script = _shell_script(argv)
        if script is None:
            return CommandScan(argv=argv, shell=True)
        inner = _scan_tokens(_split_shell(script))
        return CommandScan(
            argv=argv,
            write_paths=inner.write_paths,
            referenced_paths=inner.referenced_paths,
            actions=inner.actions,
            shell=True,
        )
    return _scan_tokens([list(argv)])


def _scan_tokens(commands: list[list[str]]) -> CommandScan:
    write_paths: list[str] = []
    referenced: list[str] = []
    actions: list[str] = []

    for tokens in commands:
        if not tokens:
            continue
        program = posixpath.basename(tokens[0])
        args = tokens[1:]

        # Every non-flag token is a *candidate* reference.  The protected
        # material is matched by pattern, so a reference without a slash
        # ("conftest.py") has to be collected too -- a path-like heuristic
        # alone would miss exactly the short names that matter.
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            referenced.append(token)
            for quoted in _QUOTED.findall(token):
                inner = quoted[0] or quoted[1]
                if not inner:
                    continue
                referenced.append(inner)
                if _looks_like_path(inner) and any(
                    marker in token for marker in _WRITE_MARKERS
                ):
                    write_paths.append(inner)
        for target in _redirection_targets(tokens):
            write_paths.append(target)
            referenced.append(target)

        if program in _DELETE_PROGRAMS:
            write_paths.extend(_non_flag(args))
            actions.append("fs.delete")
        elif program in _COPY_PROGRAMS:
            candidates = _non_flag(args)
            if candidates:
                write_paths.append(candidates[-1])
        elif program in _WRITE_PROGRAMS:
            if program == "ln":
                candidates = _non_flag(args)
                if candidates:
                    write_paths.append(candidates[-1])
            elif program == "dd":
                write_paths.extend(
                    arg.split("=", 1)[1] for arg in args if arg.startswith("of=")
                )
            else:
                write_paths.extend(_non_flag(args))
        elif program == "sed":
            if any(arg.startswith("-i") for arg in args):
                write_paths.extend(_non_flag(args)[1:])
        elif program in _NETWORK_PROGRAMS:
            actions.append("net.http")
        elif program == "git":
            _scan_git(args, write_paths, referenced, actions)

    return CommandScan(
        write_paths=tuple(dict.fromkeys(write_paths)),
        referenced_paths=tuple(dict.fromkeys(referenced)),
        actions=tuple(dict.fromkeys(actions)),
    )


def _scan_git(
    args: list[str],
    write_paths: list[str],
    referenced: list[str],
    actions: list[str],
) -> None:
    """Analyse a git invocation."""
    subcommand = ""
    for arg in args:
        if not arg.startswith("-"):
            subcommand = arg
            break
    if subcommand == "config":
        # Writing git configuration installs executable authority: a
        # ``core.hooksPath`` or an alias is code that runs with the
        # runner's privileges.  Reading it is harmless.
        if not any(arg in ("--get", "--get-all", "--get-regexp", "--list", "-l") for arg in args):
            actions.append("git.config")
        return
    if subcommand in _GIT_APPROVAL_ACTIONS:
        actions.append(_GIT_APPROVAL_ACTIONS[subcommand])
    if subcommand in _GIT_WRITE_SUBCOMMANDS:
        paths = _non_flag(args[args.index(subcommand) + 1 :]) if subcommand in args else []
        if subcommand == "add":
            write_paths.append(".")
        else:
            write_paths.extend(paths)


@dataclass
class AuditRecord:
    run_id: str
    task_id: str | None
    actor: str
    tool: str
    arguments: dict[str, Any]
    decision: Decision
    reason: str
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "actor": self.actor,
            "tool": self.tool,
            "arguments": self.arguments,
            "decision": self.decision.value,
            "reason": self.reason,
            "approval_id": self.approval_id,
        }


class PolicyEngine:
    """Authorises tool invocations against capabilities, scope and approval rules."""

    def __init__(
        self,
        *,
        sandbox: SandboxConfig | None = None,
        approval: ApprovalConfig | None = None,
        tool_rules: tuple[ToolRule, ...] | None = None,
        protected_paths: tuple[str, ...] = (),
    ) -> None:
        self.sandbox = sandbox or SandboxConfig()
        self.approval = approval or ApprovalConfig()
        self._rules: dict[str, ToolRule] = {r.name: r for r in (tool_rules or DEFAULT_TOOL_RULES)}
        self._audit: list[AuditRecord] = []
        self._protected: tuple[str, ...] = tuple(
            dict.fromkeys((*EVALUATOR_CONFIG_PATHS, *protected_paths))
        )

    # ---------------------------------------------------------------- rules

    def register_tool(self, rule: ToolRule) -> None:
        self._rules[rule.name] = rule

    def rule_for(self, tool: str) -> ToolRule | None:
        return self._rules.get(tool)

    # ------------------------------------------------------- protected paths

    @property
    def protected_paths(self) -> tuple[str, ...]:
        """The paths the worker may not change and may not read."""
        return self._protected

    def set_protected_paths(self, paths: tuple[str, ...] | list[str]) -> None:
        """Declare the externally owned material for this run.

        The material is protected in both directions: it cannot be
        rewritten, and it cannot be read either, because a worker that can
        read the oracle can write a check that satisfies it.
        """
        self._protected = tuple(dict.fromkeys((*EVALUATOR_CONFIG_PATHS, *paths)))

    def is_protected(self, path: str) -> bool:
        """Whether ``path`` is externally owned material."""
        normalised = posixpath.normpath(str(path))
        return any(_matches(normalised, pattern) for pattern in self._protected)

    # ----------------------------------------------------------- authorising

    def authorize(
        self,
        *,
        run_id: str,
        actor: str,
        capabilities: CapabilitySet,
        tool: str,
        arguments: dict[str, Any],
        task_id: str | None = None,
        scope_root: str | None = None,
        scope_patterns: tuple[str, ...] = (),
        approved: bool = False,
        grant: Any = None,
        binding: Any = None,
    ) -> PolicyDecision:
        """Decide whether ``tool`` may run.

        ``approved`` is a legacy convenience flag; ``grant`` is the
        authoritative form, because an approval is a decision about a
        *specific* action.  When a grant is supplied it must match the
        requested action exactly, or the action is refused.
        """
        rule = self._rules.get(tool)
        if rule is None:
            decision = PolicyDecision(
                decision=Decision.DENY,
                reason=f"tool {tool!r} is not registered in the policy",
            )
            self._record(run_id, task_id, actor, tool, arguments, decision)
            return decision

        missing = capabilities.missing(rule.required_capabilities)
        if missing:
            decision = PolicyDecision(
                decision=Decision.DENY,
                reason=(
                    f"missing required capabilities: {', '.join(missing)} "
                    f"(required: {', '.join(rule.required_capabilities)})"
                ),
                capabilities=rule.required_capabilities,
            )
            self._record(run_id, task_id, actor, tool, arguments, decision)
            return decision

        path_decision = self._check_path(rule, arguments, scope_root, scope_patterns)
        if path_decision is not None:
            self._record(run_id, task_id, actor, tool, arguments, path_decision)
            return path_decision

        scan: CommandScan | None = None
        if rule.name == "run_command":
            scan = scan_command(arguments.get("command") or [])
            command_decision = self._check_command(scan, scope_patterns)
            if command_decision is not None:
                self._record(run_id, task_id, actor, tool, arguments, command_decision)
                return command_decision

        needs_approval, action = self._requires_approval(rule, arguments, scan)
        if needs_approval:
            if grant is not None:
                allowed, reason = grant.authorises(binding)
                if not allowed:
                    decision = PolicyDecision(
                        decision=Decision.DENY,
                        reason=reason,
                        required_approval=True,
                        action=action,
                        capabilities=rule.required_capabilities,
                    )
                    self._record(run_id, task_id, actor, tool, arguments, decision)
                    return decision
            elif not approved:
                decision = PolicyDecision(
                    decision=Decision.REQUIRE_APPROVAL,
                    reason=f"action {action!r} requires explicit human approval",
                    required_approval=True,
                    action=action,
                    capabilities=rule.required_capabilities,
                )
                self._record(run_id, task_id, actor, tool, arguments, decision)
                return decision

        decision = PolicyDecision(
            decision=Decision.ALLOW,
            reason="authorised",
            capabilities=rule.required_capabilities,
        )
        self._record(run_id, task_id, actor, tool, arguments, decision)
        return decision

    # ------------------------------------------------------------- bindings

    def binding_for(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        workspace: str = "",
        contract_hash: str = "",
        request_version: int = 1,
        run_id: str = "",
    ) -> ApprovalBinding | None:
        """The authoritative approval binding for a tool call.

        The binding names the *exact* effect: the action and the arguments
        that define it.  A binding that carries only an action name --
        ``fs.delete`` with no path -- has not bound the approval to an
        effect at all: the same grant would then authorise the deletion of
        any path in the workspace.

        The caller is expected to compare a grant against *this* binding
        rather than against a binding it assembled itself, because the
        engine knows which arguments define the effect and a caller
        generally does not.
        """
        rule = self._rules.get(tool)
        if rule is None:
            return None
        action = rule.approval_action or rule.name
        return ApprovalBinding(
            action=action,
            arguments=self.effect_arguments(tool, arguments),
            workspace=workspace,
            contract_hash=contract_hash,
            request_version=request_version,
            run_id=run_id,
        )

    def effect_arguments(self, tool: str, arguments: dict[str, Any]) -> tuple[str, ...]:
        """The arguments that define what a tool call would do."""
        rule = self._rules.get(tool)
        if rule is None:
            return ()
        if rule.path_argument:
            raw = arguments.get(rule.path_argument)
            return (f"{rule.path_argument}={raw}",) if raw is not None else ()
        command = arguments.get("command")
        if isinstance(command, (list, tuple)):
            return tuple(str(part) for part in command)
        if isinstance(command, str):
            return (command,)
        return tuple(f"{key}={arguments[key]}" for key in sorted(arguments))

    # ---------------------------------------------------------- path checks

    def _check_path(
        self,
        rule: ToolRule,
        arguments: dict[str, Any],
        scope_root: str | None,
        scope_patterns: tuple[str, ...],
    ) -> PolicyDecision | None:
        if not rule.path_argument:
            return None
        raw = arguments.get(rule.path_argument)
        if raw is None:
            return None
        path = str(raw)
        normalised = posixpath.normpath(path)
        if normalised.startswith("..") or normalised.startswith("/"):
            return PolicyDecision(
                decision=Decision.DENY,
                reason=f"path {path!r} escapes the permitted scope",
            )
        if scope_root is not None:
            resolved = posixpath.normpath(posixpath.join(scope_root, normalised))
            if not resolved.startswith(posixpath.normpath(scope_root)):
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"path {path!r} escapes the workspace root",
                )
        for pattern in scope_patterns:
            if fnmatch.fnmatch(normalised, pattern) or normalised.startswith(
                pattern.rstrip("/") + "/"
            ):
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"path {path!r} is excluded by the task scope",
                )
        # Externally owned material is outside the worker's authority in
        # both directions: it cannot be changed, and it cannot be read,
        # because a worker that can read the oracle can satisfy it.
        if self.is_protected(normalised):
            return PolicyDecision(
                decision=Decision.DENY,
                reason=(
                    f"path {path!r} is protected verification material: it is "
                    "externally owned and outside the worker's write authority"
                ),
            )
        for protected in self.approval.protected_paths:
            if fnmatch.fnmatch(normalised, protected) or normalised.startswith(
                protected.rstrip("/") + "/"
            ):
                return PolicyDecision(
                    decision=Decision.REQUIRE_APPROVAL,
                    reason=f"path {path!r} is protected and requires human approval",
                    required_approval=True,
                    action=f"fs.protected:{normalised}",
                )
        return None

    # -------------------------------------------------------- command checks

    def _check_command(
        self, scan: CommandScan, scope_patterns: tuple[str, ...]
    ) -> PolicyDecision | None:
        """Judge a command by what it would do, not by the tool it arrived in."""
        if not scan.argv:
            return None
        for path in scan.referenced_paths:
            if self.is_protected(path):
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=(
                        f"the command refers to {path!r}, which is protected "
                        "verification material: it is externally owned and "
                        "outside the worker's authority"
                    ),
                )
        for path in scan.write_paths:
            normalised = posixpath.normpath(path)
            for pattern in scope_patterns:
                if fnmatch.fnmatch(normalised, pattern) or normalised.startswith(
                    pattern.rstrip("/") + "/"
                ):
                    return PolicyDecision(
                        decision=Decision.DENY,
                        reason=(
                            f"the command would write {path!r}, which is "
                            "excluded by the task scope"
                        ),
                    )
        if "git.config" in scan.actions:
            return PolicyDecision(
                decision=Decision.DENY,
                reason=(
                    "the command would change the repository's git "
                    "configuration, which is executable authority the worker "
                    "may not install"
                ),
            )
        return None

    def _requires_approval(
        self,
        rule: ToolRule,
        arguments: dict[str, Any],
        scan: CommandScan | None = None,
    ) -> tuple[bool, str]:
        if rule.needs_approval:
            action = rule.approval_action or rule.name
            if action == "git.commit" and not self.approval.require_approval_for_git_commit:
                return False, action
            if action == "git.push" and not self.approval.require_approval_for_git_push:
                return False, action
            if action == "net.http" and not self.approval.require_approval_for_network:
                return False, action
            if action == "fs.delete" and not self.approval.require_approval_for_delete:
                return False, action
            return True, action
        if rule.name == "run_command":
            command = arguments.get("command") or []
            if isinstance(command, str):
                command = command.split()
            executable = str(command[0]) if command else ""
            if executable == "git":
                subcommands = [str(c) for c in command[1:]]
                if "push" in subcommands and self.approval.require_approval_for_git_push:
                    return True, "git.push"
                if "commit" in subcommands and self.approval.require_approval_for_git_commit:
                    return True, "git.commit"
            if scan is not None:
                # The command performs a protected effect even though it
                # did not arrive through the tool that declares it.
                for action in scan.actions:
                    if action == "git.push" and not self.approval.require_approval_for_git_push:
                        continue
                    if action == "git.commit" and not self.approval.require_approval_for_git_commit:
                        continue
                    if action == "net.http" and not self.approval.require_approval_for_network:
                        continue
                    if action == "fs.delete" and not self.approval.require_approval_for_delete:
                        continue
                    return True, action
        return False, ""

    # ---------------------------------------------------------------- audit

    def _record(
        self,
        run_id: str,
        task_id: str | None,
        actor: str,
        tool: str,
        arguments: dict[str, Any],
        decision: PolicyDecision,
    ) -> None:
        self._audit.append(
            AuditRecord(
                run_id=run_id,
                task_id=task_id,
                actor=actor,
                tool=tool,
                arguments=_redact(arguments),
                decision=decision.decision,
                reason=decision.reason,
            )
        )

    def audit_records(self) -> list[AuditRecord]:
        return list(self._audit)

    def denials(self) -> list[AuditRecord]:
        return [r for r in self._audit if r.decision is Decision.DENY]


class PolicyDeniedError(PolicyError):
    """Raised when the policy engine refuses a tool invocation."""


_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "authorization",
    "private_key",
    "credential",
)


def _matches(path: str, pattern: str) -> bool:
    """Whether a normalised path matches a protected pattern."""
    if fnmatch.fnmatch(path, pattern):
        return True
    if path.startswith(pattern.rstrip("/") + "/"):
        return True
    # A pattern that names a directory protects everything beneath it.
    return pattern.endswith("/**") and path.startswith(pattern[:-3].rstrip("/") + "/")


def _redact(arguments: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive argument values before they reach the audit log.

    Two passes: the obvious ones (a key that *is* a secret name) and the
    subtle ones (a value that *looks* like a credential whatever its key
    is called).  The second pass is what stops a secret from travelling
    under an innocent name.
    """
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            out[key] = "***redacted***"
        elif isinstance(value, str):
            redacted, _found = redact_secrets(value)
            out[key] = redacted[:2000] + ("...[truncated]" if len(redacted) > 2000 else "")
        elif isinstance(value, (list, tuple)):
            out[key] = [
                redact_secrets(str(item))[0] if isinstance(item, str) else item
                for item in value
            ]
        else:
            out[key] = value
    return out
