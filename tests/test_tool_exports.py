"""Every name in ``agent.tools.__all__`` must be a callable tool, not a module.

``from package import name`` silently falls back to the submodule when the
package forgot to re-export the function. The import still succeeds, so a
missing line in ``__init__.py`` looks fine locally and then fails at runtime
with "The first argument must be a string or a callable with a __name__ for
tool decorator. Got <class 'module'>" — which is how the pm graph broke.
"""

from __future__ import annotations

import types

import agent.tools as tools


def test_no_export_resolves_to_a_module():
    modules = [
        name
        for name in tools.__all__
        if isinstance(getattr(tools, name), types.ModuleType)
    ]
    assert not modules, f"these are modules, not tools (missing re-export): {modules}"


def test_every_export_is_callable():
    not_callable = [name for name in tools.__all__ if not callable(getattr(tools, name))]
    assert not not_callable, f"not callable: {not_callable}"


def test_all_matches_what_the_package_actually_exposes():
    missing = [name for name in tools.__all__ if not hasattr(tools, name)]
    assert not missing, f"declared in __all__ but absent: {missing}"
