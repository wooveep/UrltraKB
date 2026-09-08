"""Exercise the installed About dialog and bundled license reader."""

from PySide6.QtWidgets import QTabWidget

from openkb.desktop.about import AboutDialog


def verify_distribution(window, root, wait):
    dialog = AboutDialog(window.io, window)
    dialog.show()
    try:
        wait(lambda: dialog.release is not None)
        release = dialog.release
        assert release.root and release.commit
        assert release.version in dialog.info.toPlainText()
        assert release.commit in dialog.info.toPlainText()
        if release.source_archive:
            assert release.source_archive.name in dialog.status.text()
            assert release.source_archive.sha256 in dialog.info.toPlainText()
            assert {file.kind for file in release.files} == {"licenses", "notice"}
        assert dialog.grab().save(str(root / "distribution-about.png"))
        dialog.findChild(QTabWidget).setCurrentIndex(1)
        assert dialog.licenses.isVisible() and dialog.license_text.isVisible()
        index = next(
            i
            for i in range(dialog.licenses.count())
            if dialog.licenses.itemText(i).endswith("NotoSansCJK-Sans2.004-OFL.txt")
        )
        dialog.licenses.setCurrentIndex(index)
        dialog.licenses.activated.emit(index)
        wait(lambda: "SIL OPEN FONT LICENSE" in dialog.license_text.toPlainText())
        assert dialog.grab().save(str(root / "distribution-license.png"))
    finally:
        dialog.close()
