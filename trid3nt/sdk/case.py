"""A case: the name a model's runs are recorded under, with one factory per registered template."""

from __future__ import annotations

from typing import Any, Callable

from trid3nt.inputs.user_input import WireArgsError

from .model import Model

__all__ = ["Case", "case"]


def _workflow(template: str) -> Any:
    """The declared workflow a registered template runs; ``None`` for a name that is no template."""
    from trid3nt.tools import TOOL_REGISTRY

    registered = TOOL_REGISTRY.get(template)
    return getattr(getattr(registered, "fn", None), "workflow", None)


class Case:
    """One case: ``case.<template>(**values)`` makes a Model over that template, each value set as an attribute is."""

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise WireArgsError(f"a case is named by a non-empty string; got {type(name).__name__}.")
        self.name = name

    def __getattr__(self, template: str) -> Callable[..., Model]:
        if template.startswith("_"):
            raise AttributeError(template)
        workflow = _workflow(template)
        if workflow is None:
            raise AttributeError("no registered template is called so; the templates are "
                                 + ", ".join(self.templates()) + ".")

        def model(**values: Any) -> Model:
            return Model(self.name, workflow, values)

        model.__name__ = model.__qualname__ = template
        model.__doc__ = getattr(_registered_fn(template), "__doc__", None)
        return model

    @staticmethod
    def templates() -> list[str]:
        """Every registered template a model can be made over, by name."""
        from trid3nt.tools import TOOL_REGISTRY

        return sorted(name for name in TOOL_REGISTRY if _workflow(name) is not None)

    def __repr__(self) -> str:
        return f"Case({self.name!r})"


def _registered_fn(template: str) -> Any:
    from trid3nt.tools import TOOL_REGISTRY

    return TOOL_REGISTRY[template].fn


def case(name: str) -> Case:
    """The case called ``name``: every model made from it records its runs under that name."""
    return Case(name)
