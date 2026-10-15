"""Keep required dialog actions on the current screen at native high DPI."""


def resize_to_screen(dialog, width, height):
    available = dialog.screen().availableGeometry()
    dialog.resize(min(width, available.width() - 40), min(height, available.height() - 40))
