from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Collection, Mapping, Optional
from unittest.mock import patch

import pygments.formatters.html
import pygments.lexers.python
from jinja2 import ext, nodes

try:
    # Jinja2 >= 3.0
    from jinja2 import pass_context  # type: ignore
except ImportError:  # pragma: no cover
    from jinja2 import contextfilter as pass_context  # type: ignore

from jinja2.runtime import Context
from markupsafe import Markup

import pdoc.markdown2

from . import docstrings
from ._compat import cache, removesuffix

lexer = pygments.lexers.python.PythonLexer()
"""
The pygments lexer used for pdoc.render_helpers.highlight.
Overwrite this to configure pygments lexing.
"""
formatter = pygments.formatters.html.HtmlFormatter(cssclass="codehilite")
"""
The pygments formatter used for pdoc.render_helpers.highlight. 
Overwrite this to configure pygments highlighting.
"""

markdown_extensions = {
    "code-friendly": None,
    "cuddled-lists": None,
    "fenced-code-blocks": None,
    "footnotes": None,
    "header-ids": None,
    "pyshell": None,
    "strike": None,
    "tables": None,
    "task_list": None,
    "toc": {"depth": 2},
}
"""
The default extensions loaded for `markdown2`.
Overwrite this to configure Markdown rendering.
"""


@cache
def highlight(code: str) -> str:
    """Highlight a piece of Python code using pygments."""
    return Markup(pygments.highlight(code, lexer, formatter))


@cache
def to_html(docstring: str) -> str:
    """
    Convert `docstring` from Markdown to HTML.
    """
    # careful: markdown2 returns a subclass of str with an extra
    # .toc_html attribute. don't further process the result,
    # otherwise this attribute will be lost.
    return pdoc.markdown2.markdown(docstring, extras=markdown_extensions)  # type: ignore


@pass_context
def to_markdown_with_context(context: Context, docstring: str) -> str:
    """
    Converts `docstring` from a custom docformat to Markdown (if necessary), and then from Markdown to HTML.
    """
    module: pdoc.doc.Module = context["module"]
    docformat: str = context["docformat"]
    return to_markdown(docstring, module, docformat)


def to_markdown(docstring: str, module: pdoc.doc.Module, default_docformat: str) -> str:
    docformat = getattr(module.obj, "__docformat__", default_docformat) or ""
    return docstrings.convert(docstring, docformat, module.source_file)


def split_identifier(all_modules: Collection[str], fullname: str) -> tuple[str, str]:
    """
    Split an identifier into a `(modulename, qualname)` tuple. For example, `pdoc.render_helpers.split_identifier`
    would be split into `("pdoc.render_helpers","split_identifier")`. This is necessary to generate links to the
    correct module.
    """
    if not fullname:
        raise ValueError("Invalid identifier.")
    if fullname in all_modules:
        return fullname, ""
    else:
        parent, _, name = fullname.rpartition(".")
        modulename, qualname = split_identifier(all_modules, parent)
        if qualname:
            return modulename, f"{qualname}.{name}"
        else:
            return modulename, name


def _relative_link(current: list[str], target: list[str]) -> str:
    if target == current:
        return f"../{target[-1]}.html"
    elif target[: len(current)] == current:
        return "/".join(target[len(current) :]) + ".html"
    else:
        return "../" + _relative_link(current[:-1], target)


@cache
def relative_link(current_module: str, target_module: str) -> str:
    """Compute the relative link to another module's HTML file."""
    if current_module == target_module:
        return ""
    return _relative_link(
        current_module.split(".")[:-1],
        target_module.split("."),
    )


def qualname_candidates(identifier: str, context_qualname: str) -> list[str]:
    end = len(context_qualname)
    ret = []
    while end > 0:
        ret.append(f"{context_qualname[:end]}.{identifier}")
        end = context_qualname.rfind(".", 0, end)
    ret.append(identifier)
    return ret


@pass_context
def linkify(context: Context, code: str, namespace: str = "") -> str:
    """
    Link all identifiers in a block of text. Identifiers referencing unknown modules or modules that
    are not rendered at the moment will be ignored.
    A piece of text is considered to be an identifier if it either contains a `.` or is surrounded by `<code>` tags.
    """

    def linkify_repl(m: re.Match):
        text = m.group(0)
        identifier = removesuffix(text, "()")

        # Check if this is a local reference within this module?
        mod: pdoc.doc.Module = context["module"]
        for qualname in qualname_candidates(identifier, namespace):
            doc = mod.get(qualname)
            if doc and context["is_public"](doc).strip():
                return f'<a href="#{qualname}">{text}</a>'

        # Find the parent module.
        try:
            module, qualname = split_identifier(context["all_modules"], identifier)
        except ValueError:
            return text
        else:
            if qualname:
                qualname = f"#{qualname}"
            return f'<a href="{relative_link(context["module"].modulename, module)}{qualname}">{text}</a>'

    # - (?!/) to not match on http://example.com/
    # - (?!</a>) to not match existing URLs.
    return Markup(
        re.sub(
            r"""
            \b
                 (?!\d)[a-zA-Z0-9_]+
            (?:\.(?!\d)[a-zA-Z0-9_]+)+
            (?:\(\))?
            \b(?!\(\))(?!</a>)(?![/#])  # foo.bar
            |
            (?<=<code>)
                 (?!\d)[a-zA-Z0-9_]+
            (?:\.(?!\d)[a-zA-Z0-9_]+)*
            (?:\(\))?
            (?=</code>(?!</a>))  # `foo` or `foo()`
            """,
            linkify_repl,
            code,
            flags=re.VERBOSE,
        )
    )


@pass_context
def link(context: Context, spec: tuple[str, str], text: Optional[str] = None) -> str:
    """Create a link for a specific `(modulename, qualname)` tuple."""
    mod: pdoc.doc.Module = context["module"]
    modulename, qualname = spec

    # Check if the object we are interested is also imported and re-exposed in the current namespace.
    doc = mod.get(qualname)
    if doc and doc.taken_from == spec and context["is_public"](doc).strip():
        if text:
            text = text.replace(f"{modulename}.", f"{mod.modulename}.")
        modulename = mod.modulename

    if mod.modulename == modulename:
        fullname = qualname
    else:
        fullname = removesuffix(f"{modulename}.{qualname}", ".")

    if qualname:
        qualname = f"#{qualname}"
    if modulename in context["all_modules"]:
        return Markup(
            f'<a href="{relative_link(context["module"].modulename, modulename)}{qualname}">{text or fullname}</a>'
        )
    return text or fullname


def edit_url(
    modulename: str, is_package: bool, mapping: Mapping[str, str]
) -> Optional[str]:
    """Create a link to edit a particular file in the used version control system."""
    for m, prefix in mapping.items():
        if m == modulename or modulename.startswith(f"{m}."):
            filename = modulename[len(m) + 1 :].replace(".", "/")
            if is_package:
                filename = f"{filename}/__init__.py".lstrip("/")
            else:
                filename += ".py"
            return f"{prefix}{filename}"
    return None


def root_module_name(all_modules: Mapping[str, pdoc.doc.Module]) -> Optional[str]:
    """
    Return the name of the (unique) top-level module, or `None`
    if no such module exists.

    For example, assuming `foo`, `foo.bar`, and `foo.baz` are documented,
    this function will return `foo`. If `foo` and `bar` are documented,
    this function will return `None` as there is no unique top-level module.
    """
    shortest_name = min(all_modules, key=len, default=None)
    prefix = f"{shortest_name}."
    all_others_are_submodules = all(
        x.startswith(prefix) or x == shortest_name for x in all_modules
    )
    if all_others_are_submodules:
        return shortest_name
    else:
        return None


def minify_css(css: str) -> str:
    """Do some very basic CSS minification."""
    css = re.sub(r"[ ]{4}|\n|(?<=[:{}]) | (?=[{}])", "", css)
    css = re.sub(
        r"/\*.+?\*/", lambda m: m.group(0) if m.group(0).startswith("/*!") else "", css
    )
    return Markup(css.replace("<style", "\n<style"))


@contextmanager
def defuse_unsafe_reprs():
    """This decorator is applied by pdoc before calling an object's repr().
    It applies some heuristics to patch our sensitive information.
    For example, `os.environ`'s default `__repr__` implementation exposes all
    local secrets.
    """
    with (patch.object(os._Environ, "__repr__", lambda self: "os.environ")):
        yield


class DefaultMacroExtension(ext.Extension):
    """
    This extension provides a new `{% defaultmacro %}` statement, which defines a macro only if it does not exist.

    For example,

    ```html+jinja
    {% defaultmacro example() %}
        test 123
    {% enddefaultmacro %}
    ```

    is equivalent to

    ```html+jinja
    {% macro default_example() %}
    test 123
    {% endmacro %}
    {% if not example %}
        {% macro example() %}
            test 123
        {% endmacro %}
    {% endif %}
    ```

    Additionally, the default implementation is also available as `default_$macroname`, which makes it possible
    to reference it in the override.
    """

    tags = {"defaultmacro"}

    def parse(self, parser):
        m = nodes.Macro(lineno=next(parser.stream).lineno)
        name = parser.parse_assign_target(name_only=True).name
        m.name = f"default_{name}"
        parser.parse_signature(m)
        m.body = parser.parse_statements(("name:enddefaultmacro",), drop_needle=True)

        if_stmt = nodes.If(
            nodes.Not(nodes.Name(name, "load")),
            [nodes.Macro(name, m.args, m.defaults, m.body)],
            [],
            [],
        )
        return [m, if_stmt]
