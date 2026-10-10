"""Cancellable read-only overview; this module never accesses Qt objects."""
import sqlite3
from .store import Archive

def read_summary(path,cancel):
    arc=Archive(path,read_only=True)
    try:
        arc.con.set_progress_handler(lambda:int(cancel.is_set()),1000)
        if cancel.is_set():raise InterruptedError('统计已取消')
        return arc.overview(),arc.batches(limit=30)
    except sqlite3.OperationalError:
        if cancel.is_set():raise InterruptedError('统计已取消') from None
        raise
    finally:arc.close()
