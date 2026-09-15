#!/usr/bin/env python3
"""GTA V process supervision: bring the game up, notice when it dies, get it back.

This is requirement 2. The crash is real and it is not rare -- an overnight run
lost 7 of 18 chunks to a game that died or hung mid-chunk, producing zero clips
each time. The strategy is not to prevent it (it is not understood) but to make
it cheap: detect it, relaunch, carry on.

★ Every check and every relaunch shells out to tools/*.sh instead of being
reimplemented here. Those scripts carry fixes that were expensive to find -- the
Rockstar process family killed by executable path rather than name, the wait for
handles to actually close, the liveness-vs-progress distinction, the pipeline
exit-status bug in the reuse fast path -- and their comments are the record of
why each one is shaped the way it is. A Python port would be a second copy of
that knowledge to keep correct, and the copy would drift.

What this module adds on top of the scripts is the part they cannot do: state
(where settings.xml lives, whether the game has been prepared), the settings.xml
edit, and a retry policy with backoff.
"""

import errno
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_TOOLS = os.path.join(_REPO, "tools")

_CLEANUP = os.path.join(_TOOLS, "gta_cleanup.sh")
_HEALTH = os.path.join(_TOOLS, "gta_health.sh")
_PROGRESS = os.path.join(_TOOLS, "gta_progress.sh")
_ENSURE = os.path.join(_TOOLS, "gta_ensure_running.sh")
_SETUP_DISPLAY = os.path.join(_TOOLS, "setup_display.sh")
_KILL_STALE = os.path.join(_TOOLS, "kill_stale_clients.sh")

_REQUIRED_SCRIPTS = (_CLEANUP, _HEALTH, _PROGRESS, _ENSURE, _SETUP_DISPLAY)

_REG = "/mnt/c/Windows/System32/reg.exe"
_CMD = "/mnt/c/Windows/System32/cmd.exe"

# ⚠ Windows executables refuse to run with a WSL working directory: cmd.exe warns
# "UNC paths are not supported" and silently falls back to C:\Windows, and reg.exe
# inherits the same confusion. Anchor every Windows call on a real Windows
# directory. gta_ensure_running.sh does exactly this for its Steam launch.
_WIN_CWD = "/mnt/c"

#: First backoff between recovery attempts, doubling, capped. Not instant: Steam
#: and the Rockstar launcher hold locks for several seconds after the game dies,
#: and an immediate relaunch races them into a second failure.
_RECOVER_BACKOFF_S = 15
_RECOVER_BACKOFF_MAX_S = 120

#: How long a forced relaunch is given to reach health before it counts as failed.
_RECOVER_DEADLINE_S = 360

_RELATIVE_SETTINGS = os.path.join("Rockstar Games", "GTA V", "settings.xml")


def _say(msg):
    print("[game] %s" % msg, flush=True)


# --------------------------------------------------------------------------
# subprocess plumbing
# --------------------------------------------------------------------------

def _kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError as exc:
        if exc.errno not in (errno.ESRCH, errno.EPERM):
            raise
        try:
            proc.kill()
        except OSError:
            pass


def _run(argv, timeout, env=None, cwd=None):
    """Run a helper to completion. Returns (returncode, combined output).

    Never raises for the helper's own failure -- a supervisor that throws when the
    game is already broken is one more thing to catch. A timeout comes back as
    returncode 124, the same convention timeout(1) uses.
    """
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=cwd, env=env, universal_newlines=True,
            # ⚠ Own process group, for the timeout path below.
            start_new_session=True)
    except OSError as exc:
        return 127, "cannot execute %s: %r" % (argv[0], exc)

    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "").strip()
    except subprocess.TimeoutExpired:
        # ⚠ proc.kill() alone is not enough here. These helpers spawn
        # powershell.exe and python3 grandchildren that inherit the stdout pipe;
        # killing only bash leaves the pipe open, and communicate()'s second call
        # then blocks forever waiting for an EOF that never comes. A supervisor
        # that hangs is worse than one that gives up, so take the whole group.
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out = ""
        return 124, (out or "").strip()


def _echo(out, limit=8, prefix="  | "):
    """Echo a helper's tail. The output IS the diagnostic -- gta_ensure_running.sh
    prints the ScriptHookV tail on failure, and that is usually the whole answer."""
    if not out:
        return
    lines = out.splitlines()
    if len(lines) > limit:
        lines = ["... (%d earlier lines)" % (len(lines) - limit)] + lines[-limit:]
    for line in lines:
        print(prefix + line, flush=True)


# --------------------------------------------------------------------------
# WSL <-> Windows paths
# --------------------------------------------------------------------------

_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.S)
_MNT_RE = re.compile(r"^/mnt/([A-Za-z])(/.*)?$", re.S)


def _to_wsl_path(path):
    """'D:/x/y' or 'D:\\x\\y' -> '/mnt/d/x/y'. A POSIX path is returned unchanged."""
    if not path:
        return path
    path = str(path)
    if path.startswith("/"):
        return path.rstrip("/") or "/"
    m = _DRIVE_RE.match(path)
    if not m:
        return path
    rest = m.group(2).replace("\\", "/")
    return ("/mnt/%s/%s" % (m.group(1).lower(), rest)).rstrip("/")


def _to_windows_path(path):
    """'/mnt/d/x/y' or 'D:/x/y' -> 'D:\\x\\y'."""
    if not path:
        return path
    path = str(path)
    m = _MNT_RE.match(path)
    if m:
        rest = (m.group(2) or "").lstrip("/")
        path = "%s:/%s" % (m.group(1).upper(), rest)
    return path.replace("/", "\\").rstrip("\\")


_WIN_ENV_CACHE = {}


def _win_env(name):
    """Read a Windows environment variable (USERPROFILE and friends)."""
    if name in _WIN_ENV_CACHE:
        return _WIN_ENV_CACHE[name]
    value = None
    if os.path.exists(_CMD):
        rc, out = _run([_CMD, "/c", "echo", "%%%s%%" % name], timeout=20, cwd=_WIN_CWD)
        out = out.strip()
        # cmd echoes the literal '%NAME%' back when the variable is not set.
        if rc == 0 and out and out != "%%%s%%" % name:
            value = out
    _WIN_ENV_CACHE[name] = value
    return value


def _expand_win_vars(value):
    def repl(m):
        got = _win_env(m.group(1))
        return got if got else m.group(0)
    return re.sub(r"%([^%\\/]+)%", repl, value)


def _registry_value(key, name):
    """One REG_SZ / REG_EXPAND_SZ value, already expanded. None if absent."""
    if not os.path.exists(_REG):
        return None
    rc, out = _run([_REG, "query", key, "/v", name], timeout=20, cwd=_WIN_CWD)
    if rc != 0:
        return None
    for line in out.replace("\r", "").splitlines():
        # "    Personal    REG_EXPAND_SZ    D:\Users\<you>\Documents"
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].lower() == name.lower() \
                and parts[1].upper().startswith("REG_"):
            return _expand_win_vars(parts[2].strip())
    return None


def _documents_dirs():
    """Candidate Windows Documents folders, best guess first.

    ★ The registry comes first because Documents is very often NOT under the user
    profile. It is frequently REDIRECTED elsewhere (OneDrive, another drive), so every
    <USERPROFILE>\\Documents guess misses and the "settings.xml not found" error
    would be wrong rather than merely unhelpful. OneDrive redirection does the
    same thing in the other direction.
    """
    out = []

    def add(p):
        if not p:
            return
        p = _to_wsl_path(p)
        if p and p not in out:
            out.append(p)

    for key in ("HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\"
                "User Shell Folders",
                "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\"
                "Shell Folders"):
        add(_registry_value(key, "Personal"))

    profile = _win_env("USERPROFILE")
    if profile:
        profile = _to_wsl_path(profile)
        add(os.path.join(profile, "Documents"))
        add(os.path.join(profile, "OneDrive", "Documents"))

    # Last resort for a machine where the interop calls are unavailable.
    try:
        for user in sorted(os.listdir("/mnt/c/Users")):
            if user in ("Public", "Default", "Default User", "All Users"):
                continue
            add("/mnt/c/Users/%s/Documents" % user)
            add("/mnt/c/Users/%s/OneDrive/Documents" % user)
    except OSError:
        pass
    return out


def clip_library_dir():
    """The Rockstar Editor's clip library (<Documents>/Rockstar Games/GTA V/videos/clips),
    as a WSL path, or "" if the game's Documents folder cannot be found.

    Resolved from the same Documents lookup as settings.xml, because that is the
    one place we know the game actually writes: Documents is redirected on many
    machines, and the library sits beside settings.xml wherever that is. The
    videos/clips leaf itself is created by the game on the first save, so its
    absence is not an error.
    """
    xml = find_settings_xml()
    if not xml:
        return ""
    return os.path.join(os.path.dirname(xml), "videos", "clips")


def find_settings_xml():
    """Absolute WSL path to the live GTA V settings.xml, or None."""
    override = os.environ.get("GTAV_SETTINGS_XML")
    if override:
        cand = _to_wsl_path(override)
        return cand if os.path.isfile(cand) else None
    for docs in _documents_dirs():
        cand = os.path.join(docs, _RELATIVE_SETTINGS)
        if os.path.isfile(cand):
            return cand
    return None


# --------------------------------------------------------------------------
# settings.xml editing
# --------------------------------------------------------------------------

def _xml_value(value):
    """GTA V writes booleans lowercase and everything else as a plain string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _edit_settings_xml(path, values):
    """Apply {element name: value} to settings.xml in place.

    Elements are matched by tag name anywhere in the tree, because the file nests
    them by section (<graphics><Tessellation/></graphics>, <video><VSync/></video>)
    and the caller should not have to know which section a knob lives in.
    Everything not named is left exactly as it was.

    Returns (changed, missing): changed maps name -> (old, new).
    """
    tree = ET.parse(path)
    root = tree.getroot()
    changed = {}
    missing = []

    for name in sorted(values):
        want = _xml_value(values[name])
        found = root.findall(".//" + name)
        if not found:
            missing.append(name)
            continue
        for el in found:
            if "value" in el.attrib:
                old = el.get("value")
                if old != want:
                    el.set("value", want)
                    changed[name] = (old, want)
            else:
                # A few entries carry their value as element text
                # (<configSource>SMC_AUTO</configSource>) rather than an attribute.
                old = (el.text or "").strip()
                if old != want:
                    el.text = want
                    changed[name] = (old, want)

    if changed:
        # ⚠ Write via a temp file in the same directory and rename. A torn
        # settings.xml is not a small problem: GTA V silently regenerates it from
        # autodetected defaults, which quietly undoes MSAA-off, VSync-off and the
        # resolution the intrinsics were derived from.
        directory = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".xml")
        os.close(fd)
        try:
            tree.write(tmp, encoding="UTF-8", xml_declaration=True)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    return changed, missing


# --------------------------------------------------------------------------
# Game
# --------------------------------------------------------------------------

class Game(object):
    """Supervises the GTA V process on behalf of the capture runner."""

    def __init__(self, settings):
        self.settings = settings
        missing = [s for s in _REQUIRED_SCRIPTS if not os.path.isfile(s)]
        if missing:
            raise RuntimeError(
                "helper scripts missing from %s: %s\n"
                "capture/game.py drives the game entirely through tools/*.sh; run "
                "from a complete checkout." % (_TOOLS, ", ".join(
                    os.path.basename(m) for m in missing)))
        self._settings_xml = None
        self._last_health = ""
        self._warn_gta_dir_mismatch()

    # -- game directory ----------------------------------------------------

    def _warn_gta_dir_mismatch(self):
        """⚠ gta_ensure_running.sh hardcodes the install path in its own `G=` line.

        If the configured gta_dir disagrees, the launch still "succeeds" but the
        freshly built plugin is installed into a different directory than the one
        Steam runs, so the capture silently uses stale code -- invisible in the
        output. Cheaper to say so at startup than to debug the frames.
        """
        want = _to_wsl_path(getattr(self.settings, "gta_dir", "") or "")
        if not want:
            return
        try:
            with open(_ENSURE, "r") as fh:
                text = fh.read()
        except OSError:
            return
        m = re.search(r'(?m)^G="([^"]+)"', text)
        if not m:
            return
        have = _to_wsl_path(m.group(1))
        if os.path.normpath(have).lower() != os.path.normpath(want).lower():
            _say("⚠ gta_dir is %s but tools/gta_ensure_running.sh installs the "
                 "plugin into %s -- one of the two is wrong" % (want, have))

    # -- settings.xml ------------------------------------------------------

    def settings_xml_path(self):
        """Locate settings.xml, raising a fixable error rather than guessing."""
        if self._settings_xml and os.path.isfile(self._settings_xml):
            return self._settings_xml
        found = find_settings_xml()
        if not found:
            override = os.environ.get("GTAV_SETTINGS_XML")
            if override:
                searched = "%s  (from GTAV_SETTINGS_XML)" % _to_wsl_path(override)
            else:
                searched = "\n  ".join(
                    os.path.join(d, _RELATIVE_SETTINGS) for d in _documents_dirs())
            raise RuntimeError(
                "GTA V settings.xml not found. Looked in:\n  %s\n"
                "Launch GTA V once so it writes the file, or point at it "
                "explicitly:  export GTAV_SETTINGS_XML='D:/path/to/settings.xml'"
                % (searched or "(no Documents folder resolved)"))
        self._settings_xml = found
        return found

    def _backup_settings_xml(self, path):
        """Copy to settings.xml.orig, once and only once.

        ★ Once matters. A second backup taken after we have already edited the
        file would preserve OUR settings as "the user's own", which is precisely
        what the backup exists to prevent.
        """
        orig = path + ".orig"
        if os.path.exists(orig):
            return
        shutil.copy2(path, orig)
        _say("backed up original settings to %s" % orig)

    def apply_display_settings(self):
        """Write the capture's graphics/video settings and make GTA V DPI-aware.

        ⚠ MUST run BEFORE the game launches. GTA V reads settings.xml exactly once
        at startup, and rewrites it from its in-memory state on exit -- so an edit
        applied to a running game is not merely late, it is discarded on the way
        out. Recovery re-applies it after cleanup for the same reason.
        """
        path = self.settings_xml_path()
        self._backup_settings_xml(path)

        values = self.settings.graphics_xml_values()
        changed, missing = _edit_settings_xml(path, values)

        if changed:
            _say("settings.xml: %d change(s) in %s" % (len(changed), path))
            for name in sorted(changed):
                old, new = changed[name]
                print("  | %-28s %s -> %s" % (name, old, new), flush=True)
        else:
            _say("settings.xml already matches the requested %d value(s)"
                 % len(values))
        if missing:
            # Not fatal -- element names drift between settings versions -- but
            # silently dropping a graphics knob is how a preset becomes fiction.
            _say("⚠ settings.xml has no element for: %s (left unset)"
                 % ", ".join(missing))

        # ★★ The DPI flag, without which GTA V renders into a virtualised surface
        # smaller than settings.xml claims and nothing anywhere reports it: the
        # plugin upsamples to the requested frame size, so the stream is the right
        # shape and meta.json's K describes a sampling grid that does not exist.
        rc, out = _run(["bash", _SETUP_DISPLAY,
                        _to_windows_path(self.settings.gta_dir)],
                       timeout=120, cwd=_WIN_CWD)
        if rc != 0:
            _say("⚠ setup_display.sh exited %d; GTA V may not be DPI-aware" % rc)
            _echo(out)
        else:
            for line in out.splitlines():
                if line.startswith("DPI-aware:"):
                    print("  | " + line, flush=True)

    # -- state -------------------------------------------------------------

    def _env(self):
        env = os.environ.copy()
        env["DEEPGTAV_HOST"] = str(self.settings.host)
        env["DEEPGTAV_PORT"] = str(self.settings.port)
        # Not read by the helpers today, which hardcode the install path; exported
        # so that when one learns to honour it, nothing here has to change.
        env["GTA_DIR"] = _to_wsl_path(self.settings.gta_dir)
        # ⚠ Legacy vs Enhanced. The default target is Steam app 271590, "Grand
        # Theft Auto V Legacy". "Grand Theft Auto V Enhanced" (app 3240220) is a
        # SEPARATE product with its own install folder that the ScriptHookV build
        # this plugin targets does not support. Set unconditionally, not just on a
        # forced relaunch, or an ordinary launch would silently fall back to the
        # hardcoded Steam default for a user on Rockstar or Epic.
        launch = getattr(self.settings, "launch_command", "")
        if launch:
            env["GTA_LAUNCH_CMD"] = str(launch)
        # ⚠ Inherited FORCE_RELAUNCH would turn every ensure_running() into a full
        # ~95 s recycle. runner.py exports it, so this is not
        # hypothetical. ensure_running() sets it deliberately or not at all.
        env.pop("FORCE_RELAUNCH", None)
        return env

    def is_alive(self):
        """Process up and control socket bound.

        ⚠ LIVENESS ONLY. A deadlocked script thread keeps the process alive and the
        socket bound while producing no frames at all, and this stays green through
        the whole thing. Never gate reuse of the game on this -- use is_producing().
        """
        rc, out = _run(["bash", _HEALTH], timeout=60, env=self._env())
        self._last_health = out
        return rc == 0

    def last_health(self):
        """The last health line, e.g. 'OK mem=3.1GB socket-open responding=True'."""
        return self._last_health

    def is_producing(self, timeout_s=20):
        """Does the plugin actually deliver a frame? This is the real check.

        ⚠ Safe only BETWEEN clips. The plugin's control socket is ZMQ PAIR, which
        accepts exactly one peer, so probing while a capture client holds it fights
        with the capture.
        """
        # ⚠ The RETURN CODE decides, never the text. gta_progress.sh prints a
        # human line on both paths ("producing frames (N bytes ...)" /
        # "no-frame ..."), and reading success out of stdout is the same class of
        # mistake as reading it out of a pipeline's exit status.
        rc, out = _run(["bash", _PROGRESS, str(int(timeout_s))],
                       timeout=int(timeout_s) + 60, env=self._env())
        tail = out.splitlines()[-1] if out else "(no output)"
        if rc == 0:
            _say("progress: %s" % tail)
        else:
            _say("no progress (rc=%d): %s" % (rc, tail))
        return rc == 0

    # -- lifecycle ---------------------------------------------------------

    def ensure_running(self, deadline_s=360, force=False):
        """Guarantee a usable game, reusing a healthy one. True if it is up.

        Delegates wholesale to tools/gta_ensure_running.sh: cleanup -> install the
        current plugin -> launch -> block until healthy, with a reuse fast path
        gated on a real frame.
        """
        deadline_s = int(deadline_s)
        env = self._env()
        if force:
            env["FORCE_RELAUNCH"] = "1"

        _say("ensuring game is up (deadline %ds%s)"
             % (deadline_s, ", forced relaunch" if force else ""))
        # The script's own budget is the health wait; cleanup (up to ~48 s), the
        # progress probe on the reuse path (~30 s) and the 45 s world settle all
        # sit outside it. Give the subprocess room for those, or the supervisor
        # kills a launch that was about to succeed.
        rc, out = _run(["bash", _ENSURE, str(deadline_s)],
                       timeout=deadline_s + 240, env=env)

        # ⚠ Success is the exit status and nothing else. The shell version of this
        # check once read a pipeline's status instead of the command's, so every
        # failed progress probe read as success and hung games were handed back to
        # the capture seven times in one night. From Python the equivalent mistake
        # is grepping stdout for "reusing" or "healthy"; do not.
        if rc == 0:
            _echo(out, limit=3)
            return True

        if rc == 124:
            _say("⚠ gta_ensure_running.sh exceeded %ds and was killed"
                 % (deadline_s + 240))
        else:
            _say("game did not come up (rc=%d)" % rc)
        _echo(out, limit=12)
        return False

    def recover(self, reason, attempts=3):
        """Force the game back after a crash or a hang. True if it came back.

        Used by the runner when a chunk fails: the death is expected, so the
        response is mechanical rather than an error path.
        """
        _say("=== recovery requested: %s ===" % reason)
        for attempt in range(1, int(attempts) + 1):
            _say("recovery attempt %d/%d" % (attempt, attempts))
            self.stop()

            # settings.xml is rewritten by GTA V on exit, so the presets have to go
            # back on now, while nothing holds the file and before the relaunch
            # reads it. Best effort: a missing settings.xml is a reason to complain,
            # not to abandon a recovery that would otherwise work.
            try:
                self.apply_display_settings()
            except Exception as exc:
                _say("⚠ could not re-apply display settings: %r" % (exc,))

            if self.ensure_running(deadline_s=_RECOVER_DEADLINE_S, force=True):
                # ⚠ ensure_running's relaunch path waits for HEALTH, which is
                # liveness. Confirm a real frame before telling the runner the game
                # is usable, or a "successful" recovery can hand back the very hang
                # it was called to fix.
                if self.is_producing(20):
                    _say("=== recovered after %d attempt(s) ===" % attempt)
                    return True
                _say("came up but is not producing frames")

            if attempt < attempts:
                back = min(_RECOVER_BACKOFF_S * (2 ** (attempt - 1)),
                           _RECOVER_BACKOFF_MAX_S)
                _say("backing off %ds before the next attempt" % back)
                time.sleep(back)

        # Three failed launches in a row is a broken install or a wedged Steam
        # client, not the transient crash this exists to absorb.
        _say("=== recovery FAILED after %d attempts: %s ===" % (attempts, reason))
        return False

    def stop(self):
        """Kill the game and anything holding its socket. Safe to call when down."""
        rc, out = _run(["bash", _CLEANUP], timeout=180)
        tail = out.splitlines()[-1] if out else "(no output)"
        if "STILL RUNNING" in out:
            # ⚠ A survivor is not cosmetic: the launcher silently refuses to start
            # while an instance is alive, and the next ensure_running() then burns
            # its entire health deadline discovering that.
            _say("⚠ cleanup left processes alive: %s" % tail)
        else:
            _say("cleanup: %s" % tail)

        # An orphaned capture client keeps the plugin's single ZMQ PAIR slot, and
        # every later client then connects "successfully" and is ignored forever.
        if os.path.isfile(_KILL_STALE):
            _run(["bash", _KILL_STALE], timeout=60)


# --------------------------------------------------------------------------
# Non-destructive self-check. Never launches the game.
#
#   python3 -m capture.game           locate settings.xml, verify the helpers
#   python3 -m capture.game --probe   also ask the plugin for a frame
#
# ⚠ --probe opens the control socket, so run it only when no capture is running.
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    class _Probe(object):
        gta_dir = os.environ.get(
            "GTA_DIR", "D:/SteamLibrary/steamapps/common/Grand Theft Auto V")
        host = os.environ.get("DEEPGTAV_HOST", "172.28.32.1")
        port = int(os.environ.get("DEEPGTAV_PORT", "8000"))

        def graphics_xml_values(self):
            return {}

    g = Game(_Probe())
    for script in _REQUIRED_SCRIPTS:
        print("  helper %-24s %s" % (os.path.basename(script),
                                     "ok" if os.path.isfile(script) else "MISSING"))
    try:
        print("  settings.xml             %s" % g.settings_xml_path())
    except RuntimeError as exc:
        print("  settings.xml             NOT FOUND\n%s" % exc)
    print("  is_alive()               %s" % g.is_alive())
    print("  last_health()            %s" % (g.last_health() or "(none)"))
    if "--probe" in sys.argv:
        print("  is_producing()           %s" % g.is_producing(10))
    sys.exit(0)
