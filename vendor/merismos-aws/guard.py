"""The refusal happens in the tool dispatcher, not in a system prompt.

Strands fires ``BeforeToolCallEvent`` immediately before it invokes a tool, and
a hook may set ``cancel_tool`` to a string. When it does, the SDK never calls
the tool and puts that string into a tool result with an error status. So a
model that has been talked into asking for the write does not get the write; it
gets a sentence explaining that the identity it is running as may not have it.

That distinction is the whole argument of this project. A prompt saying "never
publish without approval" is a request. This is a control: it is the same code
path whatever the model was told, whatever the offer text said, and whoever
wrote the offer text.

The proof that it has teeth is not in this file. It is a CI job that disables
this hook and asserts the same model then reaches the tool. A gate nobody has
watched go red is a gate nobody should believe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Tools no identity may ever call, whatever role it holds. These are names the
# fleet does not implement and must not acquire by accident: if one appears in a
# toolset, the guard refuses it rather than trusting that it does what its name
# suggests.
FORBIDDEN_EVERYWHERE = frozenset(
    {
        "delete_record",
        "rewrite_thread",
        "amend_published_record",
        "email_beneficiaries",
        "grant_role",
    }
)

# What each role may call. A role absent from this mapping may call nothing,
# which is the safe direction for a typo in a deployment variable.
#
# **These names are the toolbox's names and drift between the two is a defect in
# both directions**, so it is asserted rather than reviewed. A name here with no
# tool behind it is a permission granted over nothing; a tool with no name here
# is refused at runtime by a guard that looks like a bug. This list said
# ``list_offers``, ``read_offer`` and ``read_org``, none of which exist, and
# omitted ``list_paths`` and ``read_file``, which are how the reader reads. The
# reader could not have opened a single file in a deployment.
ROLE_TOOLS: dict[str, frozenset[str]] = {
    # The reader orchestrates the whole chore and holds no publish credential.
    "reader": frozenset(
        {
            "list_paths",
            "read_file",
            "search",
            "recall",
            "record_finding",
            "defer_until",
            "propose_allocation",
        }
    ),
    # The evaluator judges the bytes it was handed and deliberately holds no
    # read tool at all. A gate that can go looking is a gate that can be sent
    # looking, and the deterministic checks need nothing but the draft.
    "evaluator": frozenset({"record_finding"}),
    # The writer publishes an approved record and does nothing else.
    "writer": frozenset({"publish_record"}),
}

PUBLISH_TOOL = "publish_record"


@dataclass
class Refusal:
    """One refused call, kept so the thread can show what was attempted."""

    tool: str
    role: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"tool": self.tool, "role": self.role, "message": self.message}


def decide(role: str, tool_name: str) -> str | None:
    """Return a refusal message, or ``None`` to let the call through.

    Pure, synchronous and free of any SDK type, so the policy can be tested
    without constructing an agent, and so the same decision can be re-run
    against a thread entry months later to check that it would still be made.
    """
    if not tool_name:
        return "a tool call arrived with no name and was refused"
    if tool_name in FORBIDDEN_EVERYWHERE:
        return (
            f"{tool_name} is refused to every identity in this fleet, "
            f"including {role}"
        )
    allowed = ROLE_TOOLS.get(role)
    if allowed is None:
        return (
            f"the role {role!r} is not one this fleet knows, so it may call "
            f"nothing. This is a deployment fault, not a model fault"
        )
    if tool_name not in allowed:
        if tool_name == PUBLISH_TOOL:
            return (
                f"the {role} identity cannot publish a record. Publishing is the "
                f"writer's, behind a human approval bound to the exact bytes"
            )
        return f"the {role} identity may not call {tool_name}"
    return None


@dataclass
class Guard:
    """A Strands ``HookProvider`` that cancels a tool call the role may not make.

    Constructed with the role this process is deployed as, which comes from the
    environment rather than from anything a request can set. A request that
    could name its own role would be a request that could name its own
    privileges.
    """

    role: str
    on_refusal: Callable[[Refusal], None] | None = None
    refusals: list[Refusal] = field(default_factory=list)

    def register_hooks(self, registry: Any, **_kwargs: Any) -> None:
        """Subscribe to the one event that runs before a tool is invoked."""
        from strands.hooks import BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    def before_tool_call(self, event: Any) -> None:
        """Cancel the call when the role may not make it.

        ``cancel_tool`` is documented by the SDK as: a message that, when set,
        cancels the tool call and is placed into a tool result with an error
        status. Setting it is therefore the refusal, and returning early without
        setting it is the permission.
        """
        tool_name = ""
        tool_use = getattr(event, "tool_use", None)
        if isinstance(tool_use, dict):
            tool_name = str(tool_use.get("name") or "")
        elif getattr(event, "selected_tool", None) is not None:
            tool_name = str(getattr(event.selected_tool, "tool_name", "") or "")

        message = decide(self.role, tool_name)
        if message is None:
            return

        refusal = Refusal(tool=tool_name, role=self.role, message=message)
        self.refusals.append(refusal)
        if self.on_refusal is not None:
            self.on_refusal(refusal)
        event.cancel_tool = message

    def as_dict(self) -> dict[str, Any]:
        """What ``/identity`` reports about this process's tool authority."""
        return {
            "role": self.role,
            "may_call": sorted(ROLE_TOOLS.get(self.role, frozenset())),
            "refused_everywhere": sorted(FORBIDDEN_EVERYWHERE),
            "refusals_this_process": [r.as_dict() for r in self.refusals],
        }
