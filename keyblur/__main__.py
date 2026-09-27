import os
import sys

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("KeyBlur")
    app.setOrganizationName("KeyBlur")
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.lower().endswith(".keyblur"):
            win.open_project(arg)
        elif os.path.exists(arg):
            win.open_video(arg)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
