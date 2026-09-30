"""Independent olefile verification of cfb's reconstructed subtree and legal metadata."""

import olefile


def _entry(compound, path):
    # olefile 0.47 exposes CLSID through its public API; these pinned directory
    # fields also retain raw FILETIME values and state bits without rounding.
    return compound.root if not path else compound.direntries[compound._find(path)]


def verify_storage_copy(original, storage, restored, meter):
    with (
        olefile.OleFileIO(original, raise_defects=olefile.DEFECT_INCORRECT) as source,
        olefile.OleFileIO(restored, raise_defects=olefile.DEFECT_INCORRECT) as target,
    ):
        expected = {
            tuple(path[len(storage) :]): path
            for path in source.listdir(streams=True, storages=True)
            if path[: len(storage)] == list(storage)
        }
        expected[()] = list(storage)
        actual = {tuple(path) for path in target.listdir(streams=True, storages=True)} | {()}
        if set(expected) != actual or target.root.createTime != 0:
            raise ValueError("Rebuilt CFB tree or root creation time is invalid")
        for relative, source_path in expected.items():
            meter.check()
            before, after = _entry(source, source_path), _entry(target, list(relative))
            if (
                before.clsid != after.clsid
                or before.dwUserFlags != after.dwUserFlags
                or before.modifyTime != after.modifyTime
            ):
                raise ValueError("Rebuilt CFB metadata differs from its source storage")
            if relative and (
                before.entry_type != after.entry_type or before.createTime != after.createTime
            ):
                raise ValueError("Rebuilt CFB entry type or creation time changed")
            if before.entry_type == olefile.STGTY_STREAM:
                a, b = source.openstream(source_path), target.openstream(list(relative))
                if before.size != after.size:
                    raise ValueError("Rebuilt CFB stream size changed")
                while chunk := a.read(65536):
                    meter.consume(len(chunk))
                    if chunk != b.read(len(chunk)):
                        raise ValueError("Rebuilt CFB stream contents changed")
