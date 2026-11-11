"""Side-effect classification.

Not every action is the same.  Some can be repeated safely, some can be
repeated but need reconciling afterwards, and some must never be repeated
without a human.  The runtime has to know which is which, because recovery
must not blindly repeat an action whose effect it cannot undo.

The classes:

``IDEMPOTENT``
    Repeating the action produces the same result.  Writing a file with the
    same content, applying the same patch, recording a state transition.
    Recovery may repeat it freely.

``RECONCILABLE``
    Repeating the action is safe *if* the outcome is checked afterwards.
    A git commit may or may not have landed; the recovery must look, not
    assume.

``APPROVAL_REQUIRED``
    The action has an effect outside the workspace that cannot be undone.
    Pushing, deleting something the user owns, an outbound request that
    carries data.  Recovery must not repeat it; it must escalate.

Every external effect the runtime can take is registered here, so the
classification is data rather than a comment in someone's head.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class EffectClass(str, Enum):
    IDEMPOTENT = "idempotent"
    RECONCILABLE = "reconcilable"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class Effect:
    """One external effect and how it may be repeated."""

    name: str
    effect_class: EffectClass
    description: str = ""
    reconcile: str = ""
    """How a recovery can establish what actually happened."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "effect_class": self.effect_class.value,
            "description": self.description,
            "reconcile": self.reconcile,
        }


EFFECT_REGISTRY: dict[str, Effect] = {
    effect.name: effect
    for effect in (
        Effect(
            name="task.transition",
            effect_class=EffectClass.IDEMPOTENT,
            description="A task state transition.",
        ),
        Effect(
            name="artifact.write",
            effect_class=EffectClass.IDEMPOTENT,
            description="Writing a content-addressed artifact.",
        ),
        Effect(
            name="message.publish",
            effect_class=EffectClass.IDEMPOTENT,
            description="Publishing a message with a unique id.",
        ),
        Effect(
            name="worktree.create",
            effect_class=EffectClass.IDEMPOTENT,
            description="Creating a git worktree at a known revision.",
            reconcile="Check whether the worktree path already exists.",
        ),
        Effect(
            name="patch.apply",
            effect_class=EffectClass.RECONCILABLE,
            description="Applying a patch to a tree.",
            reconcile="Compare the tree with the expected result.",
        ),
        Effect(
            name="git.commit",
            effect_class=EffectClass.RECONCILABLE,
            description="A commit inside a task worktree.",
            reconcile="Read the worktree's HEAD and compare with the expected commit.",
        ),
        Effect(
            name="git.push",
            effect_class=EffectClass.APPROVAL_REQUIRED,
            description="Pushing to a remote: an effect outside the workspace.",
        ),
        Effect(
            name="file.delete",
            effect_class=EffectClass.APPROVAL_REQUIRED,
            description="Deleting a path the user owns.",
        ),
        Effect(
            name="http.request",
            effect_class=EffectClass.APPROVAL_REQUIRED,
            description="An outbound request that may carry data off the host.",
        ),
        Effect(
            name="model.download",
            effect_class=EffectClass.RECONCILABLE,
            description="Downloading model weights.",
            reconcile="Check the file's size against the expected size.",
        ),
    )
}


def classify(name: str) -> EffectClass:
    """The class of an effect, defaulting to the cautious one."""
    effect = EFFECT_REGISTRY.get(name)
    return effect.effect_class if effect else EffectClass.APPROVAL_REQUIRED


def may_repeat(name: str) -> bool:
    """Whether recovery may repeat an effect without checking."""
    return classify(name) is EffectClass.IDEMPOTENT


def may_repeat_after_reconciliation(name: str) -> bool:
    """Whether recovery may repeat an effect once it has checked."""
    return classify(name) in (EffectClass.IDEMPOTENT, EffectClass.RECONCILABLE)


def registry() -> list[dict[str, Any]]:
    return [effect.to_dict() for effect in EFFECT_REGISTRY.values()]
