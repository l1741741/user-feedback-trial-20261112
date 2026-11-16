"""
Root tag drawer.

"""

from .helpers import node_format


def svg(surface, node):
    """Draw a svg ``node``."""
    if node.parent is not None:
        # ``set_context_size`` clips to an empty region when the viewBox has a
        # zero width or height: such a viewport renders nothing.
        width, height, viewbox = node_format(surface, node)
        surface.set_context_size(width, height, viewbox, node)
