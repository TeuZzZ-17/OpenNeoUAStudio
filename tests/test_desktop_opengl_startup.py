import ast
from pathlib import Path
import unittest

from PySide6.QtGui import QSurfaceFormat

from map_editor.render.gpu_viewport import gl_format
from studio.rendering.gpu import _surface_format, request_desktop_opengl


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ENTRYPOINTS = (
    "main.py",
    "map_editor/editor.py",
    "studio/workbench/window.py",
    "wireframe_editor/window.py",
)


def _contains_direct_call(statement, function_name):
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == function_name
        for child in ast.iter_child_nodes(statement)
    )


def _startup_order_errors(body, filename):
    errors = []
    for index, statement in enumerate(body):
        if _contains_direct_call(statement, "QApplication"):
            has_prior_request = index > 0 and _contains_direct_call(
                body[index - 1], "request_desktop_opengl"
            )
            if not has_prior_request:
                errors.append(f"{filename}:{statement.lineno}")
        for field in ("body", "orelse", "finalbody"):
            nested = getattr(statement, field, None)
            if isinstance(nested, list):
                errors.extend(_startup_order_errors(nested, filename))
    return errors


class DesktopOpenGLStartupTests(unittest.TestCase):
    def test_request_preserves_default_attributes_and_selects_desktop_opengl(self):
        original = QSurfaceFormat.defaultFormat()
        custom = QSurfaceFormat(original)
        custom.setRenderableType(QSurfaceFormat.RenderableType.OpenGLES)
        custom.setDepthBufferSize(19)
        custom.setStencilBufferSize(7)
        custom.setSamples(4)
        custom.setSwapInterval(0)
        custom.setVersion(2, 1)
        custom.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        custom.setAlphaBufferSize(8)
        expected = QSurfaceFormat(custom)
        QSurfaceFormat.setDefaultFormat(custom)
        try:
            request_desktop_opengl()
            requested = QSurfaceFormat.defaultFormat()

            self.assertEqual(
                requested.renderableType(), QSurfaceFormat.RenderableType.OpenGL
            )
            self.assertEqual(requested.depthBufferSize(), expected.depthBufferSize())
            self.assertEqual(requested.stencilBufferSize(), expected.stencilBufferSize())
            self.assertEqual(requested.samples(), expected.samples())
            self.assertEqual(requested.swapInterval(), expected.swapInterval())
            self.assertEqual(requested.majorVersion(), expected.majorVersion())
            self.assertEqual(requested.minorVersion(), expected.minorVersion())
            self.assertEqual(requested.profile(), expected.profile())
            self.assertEqual(requested.alphaBufferSize(), expected.alphaBufferSize())
        finally:
            QSurfaceFormat.setDefaultFormat(original)

    def test_production_gl_formats_explicitly_request_desktop_opengl(self):
        for surface in (_surface_format(), gl_format()):
            with self.subTest(surface=surface):
                self.assertEqual(
                    surface.renderableType(), QSurfaceFormat.RenderableType.OpenGL
                )

    def test_every_production_application_creation_requests_opengl_first(self):
        errors = []
        for relative_path in PRODUCTION_ENTRYPOINTS:
            source_path = ROOT / relative_path
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            errors.extend(_startup_order_errors(tree.body, relative_path))

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
