"""Map Editor notice kept for the OpenNeoUA Studio tool selector."""

from PySide6.QtWidgets import QApplication, QMessageBox


NOTICE = (
    "Map Editor is currently being rebuilt as a standalone OpenNeoUA tool.\n\n"
    "The previous integrated version has been retired while the editor and "
    "its external asset workflow are being redesigned.\n\n"
    "A new standalone release will be provided separately when ready."
)


def show_map_editor_notice(parent=None):
    """Show the public status of the retired integrated editor."""

    QMessageBox.information(parent, "Map Editor", NOTICE)


def main(argv=None):
    """Keep the former command-line entry point as a status notice."""

    app = QApplication.instance() or QApplication([])
    show_map_editor_notice()
    return 0
