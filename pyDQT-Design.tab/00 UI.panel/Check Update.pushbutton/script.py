# -*- coding: utf-8 -*-
"""
Check Update - DQT
Tells the user whether the installed pyDQT-Design is up to date or a newer
version is available, by comparing the local git commit with the latest
commit on GitHub.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""
try: __import__("dqt_usage").log_use(__file__)  # pyDQT usage log
except Exception: pass

__title__ = "Check\nUpdate"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = "Check whether your pyDQT-Design is the latest version (compares with GitHub)."

import os
import re
import clr
clr.AddReference("System")
from System.Net import WebClient
from System.Net import ServicePointManager, SecurityProtocolType

from pyrevit import forms, script

REPO = "gjnz106/pyDQT-Design.extension"
DEFAULT_BRANCH = "main"


def _ext_root():
    """.../<Check Update.pushbutton> -> .panel -> .tab -> <...>.extension"""
    d = os.path.dirname(__file__)
    for _ in range(3):
        d = os.path.dirname(d)
    return d


def _local_sha(root):
    """Return (sha, branch) read from the extension's .git, or (None, None)."""
    git = os.path.join(root, ".git")
    head = os.path.join(git, "HEAD")
    if not os.path.isfile(head):
        return None, None
    try:
        txt = open(head).read().strip()
    except Exception:
        return None, None
    if txt.startswith("ref:"):
        ref = txt.split(" ", 1)[1].strip()          # refs/heads/main
        branch = ref.split("/")[-1]
        ref_file = os.path.join(git, *ref.split("/"))
        if os.path.isfile(ref_file):
            try:
                return open(ref_file).read().strip(), branch
            except Exception:
                pass
        packed = os.path.join(git, "packed-refs")
        if os.path.isfile(packed):
            try:
                for line in open(packed):
                    line = line.strip()
                    if line and not line.startswith("#") and line.endswith(ref):
                        return line.split(" ")[0].strip(), branch
            except Exception:
                pass
        return None, branch
    return txt, None  # detached HEAD - txt is the sha


def _http_get(url):
    ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12
    wc = WebClient()
    wc.Headers.Add("User-Agent", "pyDQT-update-check")
    wc.Headers.Add("Accept", "application/vnd.github+json")
    return wc.DownloadString(url)


def _remote_sha(branch):
    url = "https://api.github.com/repos/{}/commits/{}".format(
        REPO, branch or DEFAULT_BRANCH)
    txt = _http_get(url)
    m = re.search(r'"sha"\s*:\s*"([0-9a-f]{7,40})"', txt)
    return m.group(1) if m else None


def main():
    root = _ext_root()
    local, branch = _local_sha(root)

    if local is None:
        forms.alert(
            "Cannot read the local version - pyDQT-Design does not look "
            "like a git install.\n\nTo enable one-click updates, install "
            "pyDQT-Design through pyRevit > Extension Manager using the Git "
            "URL:\nhttps://github.com/{}.git".format(REPO),
            title="pyDQT-Design - Check Update")
        return

    try:
        remote = _remote_sha(branch)
    except Exception as ex:
        forms.alert(
            "Could not reach GitHub to check for updates.\n"
            "Check your internet connection and try again.\n\n{}".format(ex),
            title="pyDQT-Design - Check Update")
        return

    if not remote:
        forms.alert("Could not read the latest version from GitHub.",
                    title="pyDQT-Design - Check Update")
        return

    lshort, rshort = local[:7], remote[:7]
    if local.startswith(remote) or remote.startswith(local):
        forms.alert(
            "You are up to date.\n\nInstalled version: {}\n"
            "Latest on GitHub:  {}".format(lshort, rshort),
            title="pyDQT-Design - Check Update")
    else:
        forms.alert(
            "A new version of pyDQT-Design is available!\n\n"
            "Installed version: {}\n"
            "Latest on GitHub:  {}\n\n"
            "How to update:\n"
            " - pyRevit ribbon > Update, then Reload, or\n"
            " - run 'git pull' in the extension folder, then Reload."
            .format(lshort, rshort),
            title="pyDQT-Design - Check Update")


if __name__ == "__main__":
    main()
