"""P6, first test: hard links work on the REAL library mount (/docs), or the design (D26) does not hold.

This is the one test that touches the real library, and it only works inside the crawler
container with `${HOME}/Documents/equipment-crawler-docs` mounted at /docs. Anywhere else it
fails on purpose; it never skips, because a skipped check proves nothing.

It creates two names for one file inside /docs/.linkcheck/ and never removes them. It is
safe to run again: names that already exist are reused, and the check is on the link count
and inode, not on the names being new. Nothing it writes looks like a library document
(<sha>.pdf).
"""
import os

LIBRARY = "/docs"
CHECK_DIR = os.path.join(LIBRARY, ".linkcheck")


def test_the_library_is_a_real_writable_mount():
    assert os.path.isdir(LIBRARY), f"{LIBRARY} does not exist in this container"
    assert os.path.ismount(LIBRARY), f"{LIBRARY} exists but is not a mount, so it would not survive the container"
    assert os.access(LIBRARY, os.W_OK), f"{LIBRARY} is not writable by this user"


def test_a_hard_link_works_on_the_real_mount():
    assert os.path.ismount(LIBRARY), f"{LIBRARY} is not a mount in this container"
    os.makedirs(CHECK_DIR, exist_ok=True)
    source = os.path.join(CHECK_DIR, "source")
    link = os.path.join(CHECK_DIR, "link")
    try:
        with open(source, "xb") as handle:
            handle.write(b"hard link check")
    except FileExistsError:
        pass  # left by an earlier run
    try:
        os.link(source, link)
    except FileExistsError:
        pass  # left by an earlier run; the inode comparison below still decides
    a, b = os.stat(source), os.stat(link)
    assert (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino), "the two names are not the same file"
    assert a.st_nlink >= 2
    assert a.st_dev == os.stat(LIBRARY).st_dev, "the check files did not land on the mount"
