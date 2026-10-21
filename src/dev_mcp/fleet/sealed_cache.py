"""Reclaim only server projections whose owning process has ended."""
from pathlib import Path
import os
import re
import shutil
import stat
import psutil


def remove_projection(path: Path, folder: Path) -> None:
    folder=folder.resolve()
    if path.resolve().parent!=folder or path.is_symlink() or path.is_junction():
        raise ValueError('projection outside ordinary cache')
    # Inspect BEFORE recursive removal: never traverse an external reparse point.
    for base,dirs,files in os.walk(path,followlinks=False):
        for name in [*dirs,*files]:
            item=Path(base)/name
            if item.is_symlink() or item.is_junction():
                raise ValueError('projection contains link')
    def writable(fn,item,exc):
        os.chmod(item,stat.S_IWRITE)
        fn(item)
    shutil.rmtree(path,onexc=writable)


def sweep(folder: Path, limit: int = 8) -> int:
    if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
        return 0
    removed=0
    for path in folder.iterdir():
        found=re.fullmatch(r'view-(\d+)-[a-z0-9_]{8}',path.name)
        if not found or not path.is_dir() or path.is_symlink() or path.is_junction():
            continue
        try:
            owner=psutil.Process(int(found[1]))
            dead=owner.create_time()>path.stat().st_mtime+1
        except psutil.NoSuchProcess:
            dead=True
        except psutil.Error:
            continue
        if dead:
            try: remove_projection(path,folder)
            except (OSError,ValueError): continue
            removed+=1
            if removed>=limit: break
    return removed
