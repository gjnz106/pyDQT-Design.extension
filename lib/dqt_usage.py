# -*- coding: utf-8 -*-
"""
pyDQT usage log.

Every pyDQT tool calls log_use(__file__) as it starts, and each extension's
startup.py calls log_session(__file__) when Revit loads it. Each call adds
one record to the pyDQT Firestore collection: who ran what, and where -
Revit / Windows user, computer, Revit / pyRevit / extension versions, the
tool, the time, and where the extension is installed (its folder and git
remote, which is how a copied or renamed install shows up). No project
names, model content or files are sent.

The first time a tool runs for a Windows user, a notice says what is
recorded - once; usage logging cannot be turned off. Records wait in
%APPDATA%\\pyDQT\\usage_queue.jsonl until that notice has been shown, and
whenever Firestore cannot be reached (offline, not configured yet), and go
with the next record that is sent.

Nothing here may ever stop a tool: every failure is swallowed, and the
sending happens on a background thread.

The same file is in pyDQT-Manage and pyDQT-Design - keep them identical.
Setup (Firestore rules, dashboard): pyDQT-Manage/usage-tracking/README.md

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

import os
import io
import re
import json
import time
import datetime
import threading

# Firebase console > Project settings > General
FIREBASE_PROJECT_ID = "pydqt-usage"
FIREBASE_API_KEY = "AIzaSyC582PtJqTtNVagIs-qGlyDLlgcb1TfpbA"
COLLECTION = "pydqt_usage"

NOTICE_VERSION = 1
MAX_QUEUE = 2000          # records kept while offline; the oldest go first
MAX_SEND = 100            # records sent per call
MAX_TEXT = 300            # characters kept per field
TIMEOUT_MS = 8000
LOCK_STALE_S = 120

FIELDS = ("event", "tool", "extension", "ext_commit", "install_path", "git_remote",
          "revit_user", "windows_user", "domain", "machine", "revit_version",
          "pyrevit_version", "os", "session")

BUNDLE_SUFFIXES = (".extension", ".tab", ".panel", ".stack", ".pulldown",
                   ".splitbutton", ".splitpushbutton", ".pushbutton", ".smartbutton")

NOTICE_TITLE = "pyDQT - usage log"
NOTICE = (u"pyDQT records how its tools are used, to count its users and to "
          u"improve the tools. Each time a pyDQT tool runs it sends:\n\n"
          u"  -  your Revit and Windows user names and the computer name\n"
          u"  -  the Revit, pyRevit and pyDQT versions, and where pyDQT is installed\n"
          u"  -  the tool's name and the time\n\n"
          u"It does not send project names, model content or files.\n\n"
          u"Usage logging is part of pyDQT and cannot be turned off.\n"
          u"This notice is shown once.")


# ============================================================================
# WHERE THINGS ARE KEPT
# ============================================================================
def data_dir():
    """%APPDATA%\\pyDQT - created when missing."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "pyDQT")
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return folder


def _queue_path():
    return os.path.join(data_dir(), "usage_queue.jsonl")


def _notice_path():
    return os.path.join(data_dir(), "usage_notice_v{}".format(NOTICE_VERSION))


def configured():
    return bool(FIREBASE_PROJECT_ID and FIREBASE_API_KEY)


# ============================================================================
# WHAT IS RECORDED
# ============================================================================
def extension_root(path):
    """The <name>.extension folder a script or startup.py lives in, or None."""
    folder = os.path.dirname(os.path.abspath(path))
    while folder and os.path.dirname(folder) != folder:
        if folder.lower().endswith(".extension"):
            return folder
        folder = os.path.dirname(folder)
    return None


def _read(path):
    with io.open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def git_info(root):
    """(commit, origin url) of the extension's git checkout - empty strings
    when it was not installed through git (a copied folder)."""
    commit, remote = u"", u""
    git = os.path.join(root, ".git") if root else ""
    try:
        head = _read(os.path.join(git, "HEAD")).strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            ref_file = os.path.join(git, *ref.split("/"))
            if os.path.isfile(ref_file):
                commit = _read(ref_file).strip()
            elif os.path.isfile(os.path.join(git, "packed-refs")):
                for line in _read(os.path.join(git, "packed-refs")).splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and line.endswith(" " + ref):
                        commit = line.split(" ")[0]
                        break
        else:
            commit = head
    except Exception:
        pass
    try:
        config = _read(os.path.join(git, "config"))
        match = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', config)
        if match:
            remote = match.group(1)
    except Exception:
        pass
    return commit[:40], remote


def tool_name(path, root):
    """"pyDQT-Manage / 02 Inquiry / HealthCheck" - the bundle folders from the
    tab down to the button, without their suffixes."""
    rel = os.path.relpath(os.path.dirname(os.path.abspath(path)), root) if root \
        else os.path.basename(os.path.dirname(os.path.abspath(path)))
    parts = []
    for part in re.split(r"[\\/]+", rel):
        for suffix in BUNDLE_SUFFIXES:
            if part.lower().endswith(suffix):
                part = part[:-len(suffix)]
                break
        if part and part != ".":
            parts.append(part)
    return u" / ".join(parts)


def _session_id(machine):
    """Same for every record from one Revit session."""
    try:
        import clr  # noqa: F401
        from System.Diagnostics import Process
        proc = Process.GetCurrentProcess()
        return u"{}-{}-{}".format(machine, proc.Id, proc.StartTime.ToString("yyyyMMddHHmmss"))
    except Exception:
        return u"{}-{}".format(machine, os.getpid())


def _host_info():
    """(Revit user, Revit version, pyRevit version) - read on Revit's thread."""
    revit_user, revit_version, pyrevit_version = u"", u"", u""
    try:
        from pyrevit import HOST_APP
        revit_user = HOST_APP.username or u""
        revit_version = u"{} ({})".format(HOST_APP.version, HOST_APP.build)
    except Exception:
        pass
    try:
        from pyrevit import versionmgr
        pyrevit_version = versionmgr.get_pyrevit_version().get_formatted()
    except Exception:
        pass
    return revit_user, revit_version, pyrevit_version


def _os_version():
    try:
        import clr  # noqa: F401
        from System import Environment
        return Environment.OSVersion.VersionString
    except Exception:
        return u""


def make_record(event, path):
    """One record: the fields in FIELDS plus the time (UTC)."""
    root = extension_root(path)
    commit, remote = git_info(root)
    revit_user, revit_version, pyrevit_version = _host_info()
    machine = os.environ.get("COMPUTERNAME", u"")
    record = {
        "event": event,
        "tool": tool_name(path, root) if event == "tool" else u"",
        "extension": os.path.basename(root)[:-len(".extension")] if root else u"",
        "ext_commit": commit,
        "install_path": root or u"",
        "git_remote": remote,
        "revit_user": revit_user,
        "windows_user": os.environ.get("USERNAME", u""),
        "domain": os.environ.get("USERDOMAIN", u""),
        "machine": machine,
        "revit_version": revit_version,
        "pyrevit_version": pyrevit_version,
        "os": _os_version(),
        "session": _session_id(machine),
        "time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for key in FIELDS:
        value = record.get(key) or u""
        if not isinstance(value, type(u"")):
            try:
                value = value.decode("utf-8", "replace")
            except Exception:
                value = u"{}".format(value)
        record[key] = value[:MAX_TEXT]
    return record


def firestore_document(record):
    """The record as a Firestore REST document."""
    fields = dict((key, {"stringValue": record.get(key, u"")}) for key in FIELDS)
    fields["time"] = {"timestampValue": record["time"]}
    return {"fields": fields}


# ============================================================================
# SENDING
# ============================================================================
def post(record):
    """Add one record to Firestore. True when it was stored."""
    import clr  # noqa: F401
    clr.AddReference("System")
    from System.Net import HttpWebRequest, ServicePointManager, SecurityProtocolType
    from System.Text import Encoding
    url = ("https://firestore.googleapis.com/v1/projects/{}/databases/(default)/documents/{}?key={}"
           .format(FIREBASE_PROJECT_ID, COLLECTION, FIREBASE_API_KEY))
    body = Encoding.UTF8.GetBytes(json.dumps(firestore_document(record)))
    ServicePointManager.SecurityProtocol = ServicePointManager.SecurityProtocol | SecurityProtocolType.Tls12
    request = HttpWebRequest.Create(url)
    request.Method = "POST"
    request.ContentType = "application/json"
    request.Timeout = TIMEOUT_MS
    request.ContentLength = body.Length
    stream = request.GetRequestStream()
    try:
        stream.Write(body, 0, body.Length)
    finally:
        stream.Close()
    response = request.GetResponse()
    try:
        return 200 <= int(response.StatusCode) < 300
    finally:
        response.Close()


def load_queue():
    try:
        records = []
        for line in _read(_queue_path()).splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
        return records
    except Exception:
        return []


def save_queue(records):
    records = records[-MAX_QUEUE:]
    with io.open(_queue_path(), "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(u"{}\n".format(json.dumps(record)))


def _take_lock():
    """True when this call may send the queue - only one at a time."""
    path = os.path.join(data_dir(), "usage_queue.lock")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) > LOCK_STALE_S:
            os.remove(path)
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except Exception:
        return False


def _drop_lock():
    try:
        os.remove(os.path.join(data_dir(), "usage_queue.lock"))
    except Exception:
        pass


def send(records):
    """Send the waiting records and these, oldest first; whatever is not
    sent waits in the queue. Runs on the background thread."""
    if not notice_shown() or not configured() or not _take_lock():
        save_queue(load_queue() + records)
        return 0
    try:
        pending = load_queue() + records
        sent = 0
        for record in pending[:MAX_SEND]:
            try:
                ok = post(record)
            except Exception:
                ok = False
            if not ok:
                break
            sent += 1
        save_queue(pending[sent:])
        return sent
    finally:
        _drop_lock()


def in_background(func, *args):
    """Run func(*args) on a background thread; never raises."""
    def run():
        try:
            func(*args)
        except Exception:
            pass
    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()
    return thread


# ============================================================================
# THE ONE-TIME NOTICE
# ============================================================================
def notice_shown():
    try:
        return os.path.isfile(_notice_path())
    except Exception:
        return False


def show_notice():
    """The notice, once per Windows user. True once it has been shown."""
    if notice_shown():
        return True
    try:
        import clr  # noqa: F401
        clr.AddReference("PresentationFramework")
        from System.Windows import MessageBox, MessageBoxButton, MessageBoxImage
        MessageBox.Show(NOTICE, NOTICE_TITLE, MessageBoxButton.OK, MessageBoxImage.Information)
    except Exception:
        return False
    try:
        with io.open(_notice_path(), "w", encoding="utf-8") as handle:
            handle.write(datetime.datetime.utcnow().strftime(u"%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        pass
    return True


# ============================================================================
# ENTRY POINTS
# ============================================================================
def log_use(script_path):
    """A pyDQT tool started - call first thing in its script.py."""
    try:
        show_notice()
        record = make_record("tool", script_path)
        in_background(send, [record])
    except Exception:
        pass


def log_session(startup_path):
    """Revit loaded a pyDQT extension - call from its startup.py."""
    try:
        record = make_record("session", startup_path)
        in_background(send, [record])
    except Exception:
        pass
