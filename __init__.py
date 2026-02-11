"""
Package Manager for Blender.

This module provides a UI panel and operators to manage Python packages
directly within Blender, supporting PyPI search, installation, and
requirements.txt bulk installation.
"""

__author__ = "Kent Edoloverio"
__version__ = "1.4.0"
__description__ = "A panel for managing Python packages directly within Blender."

import bpy
import re
import logging
import sys
import os
import subprocess
import site
import time
import json
import threading
import urllib.request
import urllib.error
from importlib.metadata import distributions, distribution, PackageNotFoundError
from bpy_extras.io_utils import ImportHelper

# ---------------------------------------------------------------------------
# Legacy Addon Metadata (for Blender < 4.2 or legacy install fallback)
# ---------------------------------------------------------------------------

bl_info = {
    "name": "Package Manager",
    "blender": (4, 2, 0),
    "version": (1, 4, 0),
    "category": "Text Editor",
    "author": "Kent Edoloverio",
    "location": "Text Editor > Package Manager",
    "description": "A panel for managing Python packages directly within Blender.",
    "warning": "Once the package is installed/removed it requires restart to apply changes",
    "tracker_url": "https://github.com/kents00/Package-Manager/issues",
    "wiki_url": "https://github.com/kents00/Package-Manager",
}

# ---------------------------------------------------------------------------
# Caching & state
# ---------------------------------------------------------------------------

_pip_ensured = False
_installed_cache = None
_pypi_cache = {}        # {query: (timestamp, result)}
_etag_cache = {}        # {url: etag_value}

CACHE_TTL = 300         # 5 minutes
SEARCH_COOLDOWN = 2     # seconds between searches
REQUEST_TIMEOUT = 10    # seconds
MAX_CACHE_SIZE = 100    # Maximum number of items in _pypi_cache
SEARCH_INSTALLED_LABEL = "Search Installed Packages"

_last_search_time = 0

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_package_name(name):
    """Validate package name to prevent injection/invalid characters."""
    if not name or not re.match(r"^[a-zA-Z0-9_\-]+$", name):
        raise ValueError(f"Invalid package name: {name}")


# ---------------------------------------------------------------------------
# PyPI helpers
# ---------------------------------------------------------------------------


def _clean_url(raw):
    """Internal helper to clean and validate a single URL string."""
    if not isinstance(raw, str):
        return None
    u = raw.strip()
    if not u:
        return None

    # Fix common typo "https//" or "http//"
    if u.startswith("https//"):
        u = "https://" + u[7:]
    elif u.startswith("http//"):
        u = "http://" + u[6:]

    # Basic URL validation: must start with http and contain a dot
    if u.startswith(("http://", "https://")) and "." in u:
        return u.rstrip(",").rstrip(";").rstrip(".")
    return None


def _format_author(name, email):
    """Format author name and email consistently."""
    n = (name or "").strip()
    e = (email or "").strip()
    if n and e:
        return f"{n} <{e}>"
    return n or e or "Unknown"


def _get_url_candidates(url_data):
    """Gather (label, url) pairs from various input formats."""
    if isinstance(url_data, dict):
        return list(url_data.items())
    candidates = []
    if isinstance(url_data, (list, tuple)):
        for item in url_data:
            if isinstance(item, str) and "," in item:
                parts = item.split(",", 1)
                candidates.append((parts[0].strip(), parts[1].strip()))
            else:
                candidates.append(("", str(item)))
    elif isinstance(url_data, str):
        candidates.extend([("", p) for p in re.split(r'[,\s]+', url_data)])
    return candidates


def extract_best_url(url_data):
    """
    Extract relevant URLs (home/repo and documentation) from various formats.
    Returns a dict: {"home": str, "docs": str}
    """
    candidates = _get_url_candidates(url_data)
    results = {"home": "", "docs": ""}
    docs_keywords = {"documentation", "docs", "wiki", "changelog"}

    cleaned_homes = []
    cleaned_docs = []

    for label, raw in candidates:
        u = _clean_url(raw)
        if not u:
            continue
        if any(kw in label.lower() for kw in docs_keywords):
            cleaned_docs.append(u)
        else:
            cleaned_homes.append(u)

    if cleaned_homes:
        gh = [u for u in cleaned_homes if "github.com" in u.lower()]
        results["home"] = gh[0] if gh else cleaned_homes[0]

    if cleaned_docs:
        results["docs"] = cleaned_docs[0]

    return results


def _fetch_pypi_json(url):
    """Network wrapper for PyPI JSON API with ETag support."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if url in _etag_cache:
        req.add_header("If-None-Match", _etag_cache[url])

    try:
        # skipcq: BAN-B310
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            etag = response.headers.get("ETag")
            if etag:
                _etag_cache[url] = etag
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise RuntimeError(f"PyPI Error {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def _parse_pypi_result(info):
    """Transform PyPI info dict into our internal package format."""
    urls = info.get("project_urls") or {}
    if info.get("home_page"):
        urls["Homepage"] = info["home_page"]
    if info.get("project_url"):
        urls["Project"] = info["project_url"]

    extracted = extract_best_url(urls)
    author = _format_author(
        info.get("author") or info.get("maintainer"),
        info.get("author_email") or info.get("maintainer_email")
    )

    return [{
        "name": info["name"],
        "author": author,
        "version": info["version"],
        "summary": info.get("summary") or "No description available",
        "home_page": extracted["home"],
        "docs_url": extracted["docs"],
        "pkg_license": info.get("license") or "N/A",
    }]


def search_pypi(query):
    """Fetch package info from PyPI with TTL cache, ETag support, and timeout."""
    now = time.time()
    if query in _pypi_cache:
        cached_time, cached_result = _pypi_cache[query]
        if now - cached_time < CACHE_TTL:
            return cached_result

    url = f"https://pypi.org/pypi/{query}/json"
    data = _fetch_pypi_json(url)

    if data is None:  # 304 Not Modified
        return _pypi_cache[query][1]

    result = _parse_pypi_result(data["info"])

    if len(_pypi_cache) >= MAX_CACHE_SIZE:
        _pypi_cache.pop(next(iter(_pypi_cache)))
    _pypi_cache[query] = (now, result)
    return result

# ---------------------------------------------------------------------------
# pip helpers
# ---------------------------------------------------------------------------


def ensure_pip():
    """Run ensurepip only once per session."""
    global _pip_ensured  # skipcq: PYL-W0603
    if _pip_ensured:
        return
    python_exec = sys.executable
    try:
        subprocess.check_call(
            [python_exec, "-m", "ensurepip", "--default-pip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        pass  # pip may already be present
    _pip_ensured = True


def _handle_pip_error(package, result, operation="installation"):
    """Analyze pip output to provide helpful error suggestions."""
    err = (result.stderr or "") + (result.stdout or "")
    if not err:
        logger.error("Error during %s of %s: exit code %d",
                     operation, package, result.returncode)
        return

    # List of (keywords, message) pairs
    patterns = [
        (["pkg-config"],
         f"Missing system tool: 'pkg-config'. {package} requires external C libraries."),
        (["Microsoft Visual C++", "cl.exe"],
         f"Missing build tools: {package} requires 'Microsoft Visual C++ Build Tools'."),
        (["Requires-Python"],
         f"Version mismatch: {package} is not compatible with this version of Python in Blender."),
        (["Could not find a version"],
         f"Not found: Could not find a version that satisfies the requirement '{package}'."),
        (["Conflicting dependencies", "ResolutionImpossible"],
         f"Dependency conflict: {package} has requirements that conflict with other installed packages."),
        (["ReadTimeoutError", "timed out"],
         f"Network timeout: The connection to PyPI timed out while downloading {package}."),
        (["SSL", "connection", "ProxyError"],
         f"Network error: Failed to download {package}. Check your internet/proxy settings."),
        (["No space left on device"],
         "Disk full: No space left on device to install the package."),
        (["PermissionError", "Access is denied"],
         f"Permission denied: Try running Blender as Administrator to {operation} {package}."),
        (["git not found", "mercurial not found", "svn not found"],
         f"Missing tool: A version control tool (git/hg/svn) required by {package} is not installed."),
    ]

    for keywords, msg in patterns:
        if any(kw.lower() in err.lower() for kw in keywords):
            logger.error(msg)
            return

    logger.error("Error during %s of %s:\n%s", operation, package, err)


def install_package(package):
    """Install a single package via pip (ensures pip once, not per call)."""
    try:
        try:
            distribution(package)
            logger.info("%s is already installed.", package)
            return True
        except PackageNotFoundError:
            pass
        logger.info("%s not found. Installing...", package)
        python_exec = sys.executable

        try:
            ensure_pip()
            result = subprocess.run(
                [python_exec, "-m", "pip", "install", "--user", package],
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode != 0:
                _handle_pip_error(package, result)
                return False

            user_site = site.getusersitepackages()
            if user_site not in sys.path:
                sys.path.append(user_site)
                logger.info("Added %s to sys.path", user_site)

            # Verify installation via metadata check (case-insensitive for package name)
            try:
                distribution(package)
                _installed_cache = None  # invalidate cache
                logger.info("%s installed successfully.", package)
                return True
            except PackageNotFoundError:
                logger.error(
                    "Failed to verify installation of %s after pip reported success.", package)
                return False
        except subprocess.CalledProcessError as e:
            logger.error("Error during installation of %s: %s", package, e)
            return False
    except Exception as e:
        logger.error(
            "Unexpected error during installation of %s: %s", package, e)
        return False


def install_packages_from_requirements(file_path):
    """Install all packages from a requirements file in a single pip call."""
    if not os.path.isfile(file_path):
        logger.warning("Requirements file not found: %s", file_path)
        return False

    with open(file_path, "r") as f:
        requirements = [
            r.strip()
            for r in f
            if r.strip() and not r.startswith("#")
        ]

    if not requirements:
        logger.warning("No packages found in requirements file.")
        return False

    ensure_pip()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user"] + requirements,
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            _handle_pip_error("requirements file", result, "bulk installation")
            return False

        logger.info("All packages installed successfully.")
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Error during bulk installation: %s", e)
        return False

# ---------------------------------------------------------------------------
# Installed packages helpers
# ---------------------------------------------------------------------------


def get_installed_packages(force_refresh=False):
    """Return cached list of installed packages; refresh on demand."""
    global _installed_cache  # skipcq: PYL-W0603
    if _installed_cache is not None and not force_refresh:
        return _installed_cache

    user_site = site.getusersitepackages().lower()

    _installed_cache = []
    for d in distributions():
        meta = d.metadata
        urls = []
        if meta.get("Home-page"):
            urls.append(meta.get("Home-page"))
        project_urls = meta.get_all("Project-URL")
        if project_urls:
            urls.extend(project_urls)

        extracted = extract_best_url(urls)

        # Detect if it's a system package (not in user site-packages)
        location = str(d.locate_file("")).lower()
        is_system = user_site not in location

        _installed_cache.append({
            "name": meta["Name"],
            "version": meta["Version"],
            "author": _format_author(meta.get("Author"), meta.get("Author-email")),
            "summary": meta.get("Summary") or "No description available",
            "home_page": extracted["home"],
            "docs_url": extracted["docs"],
            "is_system": is_system,
        })

    return _installed_cache


def uninstall_package(package):
    """Uninstall a package via pip and invalidate cache."""
    global _installed_cache  # skipcq: PYL-W0603
    python_exec = sys.executable

    try:
        installed_packages = get_installed_packages(force_refresh=True)
        installed_names = [pkg["name"].lower() for pkg in installed_packages]

        if package.lower() not in installed_names:
            logger.warning(
                "Package %s not found. Skipping uninstall.", package)
            return False

        # Use run instead of check_call to capture output for better error detection
        result = subprocess.run(
            [python_exec, "-m", "pip", "uninstall", "-y", package],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            err = result.stderr or result.stdout or ""
            if "PermissionError" in err or "Access is denied" in err:
                logger.error("Permission denied: Could not uninstall %s. "
                             "You may need to run Blender as Administrator if it's in a system directory.", package)
            else:
                logger.error("Error during uninstallation: %s", err)
            return False

        _installed_cache = None  # invalidate cache
        logger.info("%s uninstalled successfully.", package)
        return True
    except Exception as e:
        logger.error("Unexpected error during uninstallation: %s", e)
        return False


# ---------------------------------------------------------------------------
# Auto-update logic
# ---------------------------------------------------------------------------

_update_timer_registered = False


def check_package_update(package_name):
    """Check PyPI for a newer version of a package."""
    try:
        url = f"https://pypi.org/pypi/{package_name}/json"
        data = _fetch_pypi_json(url)
        if data:
            return data["info"]["version"]
    except Exception:
        pass
    return None


def update_checker_timer():
    """Timer callback to trigger background update checks."""
    # This timer runs in the main thread, so it can safely access bpy.context.scene
    bg_update_check()
    return 21600  # Run every 6 hours


def bg_update_check():
    """Trigger background check for all installed packages with auto-update on."""
    packages_to_check = []

    # This must run in main thread (context access)
    for pkg in bpy.context.scene.installed_packages:
        if pkg.auto_update:
            packages_to_check.append((pkg.name, pkg.version))

    def _worker(pkgs):
        """Background thread worker to check for updates on PyPI."""
        results = []
        for name, current_v in pkgs:
            latest_v = check_package_update(name)
            if latest_v and latest_v != current_v:
                results.append((name, latest_v))

        # Schedule the UI update back on main thread
        if results:
            bpy.app.timers.register(
                lambda: apply_update_results(results), first_interval=0.1)

    threading.Thread(target=_worker, args=(
        packages_to_check,), daemon=True).start()
    return 21600  # 6 hours


def apply_update_results(results):
    """Apply found updates to the property groups (Main Thread)."""
    for name, latest_v in results:
        for pkg in bpy.context.scene.installed_packages:
            if pkg.name == name:
                pkg.latest_version = latest_v
                pkg.update_available = True
                break

# ---------------------------------------------------------------------------
# UI — Panel
# ---------------------------------------------------------------------------


class PackageManagementPanel(bpy.types.Panel):
    """Creates a Panel in the Text Editor side panel"""
    bl_label = "Package Manager"
    bl_idname = "TEXT_PT_package_manager"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_category = "Package Manager"

    @staticmethod
    def draw_package_box(layout, package, is_installed=False, installed_set=None):
        """Helper to draw a single package information box."""
        box = layout.box()
        row = box.row()
        row.label(text=package.name, icon="FILE_SCRIPT")
        row.label(text=f"v{package.version}")

        if is_installed and package.is_system:
            row.label(text="System", icon="ERROR")

        box.row().label(text=f"Author: {package.author}", icon="USER")
        box.row().label(text=package.description, icon="INFO")

        row = box.row()
        if is_installed and package.update_available:
            row.label(
                text=f"New version available: v{package.latest_version}", icon="SOLO_ON")
            row.operator("wm.download_package", text="Update Package",
                         icon="UGLYPACKAGE").package_name = package.name

        row = box.row()
        if package.url or package.docs_url:
            if package.url:
                op = row.operator("wm.url_open", text="Website", icon="URL")
                op.url = package.url
            if package.docs_url:
                op = row.operator("wm.url_open", text="Docs", icon="BOOKMARKS")
                op.url = package.docs_url

        if is_installed:
            row.operator("wm.uninstall_package", text="Uninstall",
                         icon="TRASH").package_name = package.name
            row.prop(package, "auto_update", text="Auto Update")
        else:
            if package.name.lower() in (installed_set or set()):
                row.label(text="Installed", icon="CHECKMARK")
            else:
                row.operator("wm.download_package", text="Download",
                             icon="IMPORT").package_name = package.name

    def draw(self, context):
        """Draw the panel UI."""
        layout = self.layout
        scene = context.scene

        layout.label(text="Search Packages:")
        row = layout.row()
        row.prop(scene, "search_query", text="")
        row.operator("wm.search_packages", text="Search")

        layout.label(text="Results:")
        box = layout.box()
        row = box.row()
        row.prop(scene, "show_search_results", text="Show Search Results",
                 icon="TRIA_DOWN" if scene.show_search_results else "TRIA_RIGHT")

        if scene.show_search_results:
            if scene.package_list:
                installed_set = {pkg["name"].lower()
                                 for pkg in get_installed_packages()}
                for package in scene.package_list:
                    self.draw_package_box(
                        layout, package, installed_set=installed_set)
            else:
                box.label(text="No results found.")

        layout.separator()
        row = layout.row(align=True)
        row.label(text="Installed Packages:")
        row.operator("wm.refresh_installed_packages",
                     text="", icon="FILE_REFRESH")

        layout.row().prop(scene, "installed_search_query", text=SEARCH_INSTALLED_LABEL)
        box = layout.box()
        row = box.row()
        row.prop(scene, "show_installed_packages", text="Show Installed Packages",
                 icon="TRIA_DOWN" if scene.show_installed_packages else "TRIA_RIGHT")

        if scene.show_installed_packages:
            query = scene.installed_search_query.lower()
            filtered = [
                pkg for pkg in scene.installed_package_list if query in pkg.name.lower()]
            if filtered:
                for package in filtered:
                    self.draw_package_box(layout, package, is_installed=True)
            else:
                box.label(text="No installed packages match the search query.")

        layout.separator()
        layout.label(text="Bulk Download Packages:")
        layout.prop(scene, "bulk_download_path", text="Selected Path")
        row = layout.row()
        row.operator("wm.file_select", text="Choose File Path")
        row.operator("wm.bulk_download_packages", text="Download All")

# ---------------------------------------------------------------------------
# UI — Operators
# ---------------------------------------------------------------------------


class WM_OT_FileSelect(bpy.types.Operator, ImportHelper):
    """Operator to open the file browser"""
    bl_idname = "wm.file_select"
    bl_label = "Select Bulk Download Path"

    filter_glob: bpy.props.StringProperty(default="*", options={"HIDDEN"})

    def execute(self, context):
        """Update the scene property with the selected file path."""
        context.scene.bulk_download_path = self.filepath
        self.report({"INFO"}, f"Selected path: {self.filepath}")
        return {"FINISHED"}


class WM_OT_SearchPackages(bpy.types.Operator):
    """Search PyPI for packages (with debounce and caching)"""
    bl_idname = "wm.search_packages"
    bl_label = "Search Packages"

    _thread = None
    _results = None
    _error = None

    def modal(self, context, event):
        """Handle the modal execution state (waiting for thread)."""
        if self._thread and not self._thread.is_alive():
            context.scene.package_list.clear()
            if self._error:
                self.report(
                    {"ERROR"}, f"Error fetching results: {self._error}")
            elif self._results:
                for result in self._results:
                    item = context.scene.package_list.add()
                    item.name = result["name"]
                    item.version = result["version"]
                    item.version = result["version"]
                    item.author = result["author"]
                    item.description = result["summary"]
                    item.url = result["home_page"]
                    item.docs_url = result.get("docs_url", "")
                    item.auto_update = False
                self.report(
                    {"INFO"}, f"Found {len(self._results)} packages."
                )
            else:
                self.report({"INFO"}, "No results found.")
            context.area.tag_redraw()
            return {"FINISHED"}
        return {"PASS_THROUGH"}

    def execute(self, context):
        """Start the search in a background thread."""
        global _last_search_time  # skipcq: PYL-W0603

        # Debounce
        now = time.time()
        if now - _last_search_time < SEARCH_COOLDOWN:
            self.report({"WARNING"}, "Please wait before searching again.")
            return {"CANCELLED"}
        _last_search_time = now

        query = context.scene.search_query
        if not query.strip():
            self.report({"WARNING"}, "Please enter a search query.")
            return {"CANCELLED"}

        self._results = None
        self._error = None

        def _search():
            """Background thread worker to perform PyPI search."""
            try:
                self._results = search_pypi(query)
            except Exception as e:
                self._error = str(e)

        self._thread = threading.Thread(target=_search, daemon=True)
        self._thread.start()
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}


class WM_OT_RefreshInstalledPackages(bpy.types.Operator):
    """Refresh the list of installed packages (forces cache refresh)"""
    bl_idname = "wm.refresh_installed_packages"
    bl_label = "Refresh Installed Packages"

    def execute(self, context):
        """Refresh the installed packages list."""
        context.scene.installed_package_list.clear()
        installed_packages = get_installed_packages(force_refresh=True)

        for package in installed_packages:
            item = context.scene.installed_package_list.add()
            item.name = package["name"]
            item.version = package["version"]
            item.author = package["author"]
            item.description = package["summary"]
            item.url = package["home_page"]
            item.docs_url = package.get("docs_url", "")
            item.is_system = package.get("is_system", False)

        self.report(
            {"INFO"}, f"Found {len(installed_packages)} installed packages."
        )
        return {"FINISHED"}


class WM_OT_DownloadPackage(bpy.types.Operator):
    """Download and install a package in a background thread"""
    bl_idname = "wm.download_package"
    bl_label = "Download Package"

    package_name: bpy.props.StringProperty()

    _thread = None
    _result = None

    def modal(self, context, event):
        """Handle the modal execution state (waiting for thread)."""
        if self._thread and not self._thread.is_alive():
            if self._result:
                self.report(
                    {"INFO"}, f"{self.package_name} installed successfully."
                )
                # Clear update flag if it was an upgrade
                for item in context.scene.installed_package_list:
                    if item.name == self.package_name:
                        item.update_available = False
                        # Force a refresh to get the new version number
                        bpy.ops.wm.refresh_installed_packages()
                        break
            else:
                self.report(
                    {"ERROR"}, f"Failed to install {self.package_name}."
                )
            context.area.tag_redraw()
            return {"FINISHED"}
        return {"PASS_THROUGH"}

    def execute(self, context):
        """Start the install in a background thread."""
        self._result = None

        def _install():
            """Background thread worker to install a package."""
            try:
                validate_package_name(self.package_name)
                self._result = install_package(self.package_name)
            except ValueError as e:
                logger.error("Validation error: %s", e)
                self._result = False

        self._thread = threading.Thread(target=_install, daemon=True)
        self._thread.start()
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, f"Installing {self.package_name}...")
        return {"RUNNING_MODAL"}


class WM_OT_UninstallPackage(bpy.types.Operator):
    """Uninstall a package"""
    bl_idname = "wm.uninstall_package"
    bl_label = "Uninstall Package"

    package_name: bpy.props.StringProperty()

    def execute(self, context):
        """Uninstall the selected package."""
        package_name = self.package_name

        try:
            validate_package_name(package_name)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        if uninstall_package(package_name):
            self.report({"INFO"}, f"{package_name} uninstalled successfully.")
        else:
            self.report({"ERROR"}, f"Failed to uninstall {package_name}.")
        return {"FINISHED"}


class WM_OT_BulkDownloadPackages(bpy.types.Operator):
    """Bulk install packages from a requirements file in a background thread"""
    bl_idname = "wm.bulk_download_packages"
    bl_label = "Bulk Download Packages"

    _thread = None
    _result = None

    def modal(self, context, event):
        """Handle the modal execution state (waiting for thread)."""
        if self._thread and not self._thread.is_alive():
            file_path = context.scene.bulk_download_path
            if self._result:
                self.report(
                    {"INFO"},
                    f"Packages from {file_path} installed successfully.",
                )
            else:
                self.report(
                    {"ERROR"},
                    f"Failed to install packages from {file_path}.",
                )
            context.area.tag_redraw()
            return {"FINISHED"}
        return {"PASS_THROUGH"}

    def execute(self, context):
        """Start the bulk install in a background thread."""
        file_path = context.scene.bulk_download_path
        if not file_path:
            self.report({"ERROR"}, "No file path selected.")
            return {"CANCELLED"}

        self._result = None

        def _bulk():
            """Background thread worker to perform bulk installation."""
            self._result = install_packages_from_requirements(file_path)

        self._thread = threading.Thread(target=_bulk, daemon=True)
        self._thread.start()
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, f"Installing packages from {file_path}...")
        return {"RUNNING_MODAL"}


class WM_OT_SearchInstalledPackages(bpy.types.Operator):
    """Filter the installed packages list by search query"""
    bl_idname = "wm.search_installed_packages"
    bl_label = SEARCH_INSTALLED_LABEL

    def execute(self, context):  # skipcq: PYL-R0201
        """Filter the list based on search query."""
        search_query = context.scene.installed_search_query.lower()
        for item in context.scene.installed_package_list:
            item.hide = search_query not in item.name.lower()
        return {"FINISHED"}

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class PackageItem(bpy.types.PropertyGroup):
    """Property group representing a package in the list."""
    name: bpy.props.StringProperty()
    version: bpy.props.StringProperty()
    author: bpy.props.StringProperty()
    description: bpy.props.StringProperty()
    url: bpy.props.StringProperty()
    docs_url: bpy.props.StringProperty()
    auto_update: bpy.props.BoolProperty()
    latest_version: bpy.props.StringProperty()
    update_available: bpy.props.BoolProperty(default=False)
    is_system: bpy.props.BoolProperty()
    hide: bpy.props.BoolProperty(default=False)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


classes = (
    PackageItem,
    PackageManagementPanel,
    WM_OT_SearchPackages,
    WM_OT_RefreshInstalledPackages,
    WM_OT_DownloadPackage,
    WM_OT_UninstallPackage,
    WM_OT_BulkDownloadPackages,
    WM_OT_FileSelect,
    WM_OT_SearchInstalledPackages,
)


def register():
    """Register all classes and properties."""
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.search_query = bpy.props.StringProperty(
        name="Search Query"
    )
    bpy.types.Scene.bulk_download_path = bpy.props.StringProperty(
        name="Bulk Download Path"
    )
    bpy.types.Scene.package_list = bpy.props.CollectionProperty(
        type=PackageItem
    )
    bpy.types.Scene.installed_package_list = bpy.props.CollectionProperty(
        type=PackageItem
    )
    bpy.types.Scene.show_search_results = bpy.props.BoolProperty(
        name="Show Search Results", default=True
    )
    bpy.types.Scene.show_installed_packages = bpy.props.BoolProperty(
        name="Show Installed Packages", default=True
    )
    bpy.types.Scene.installed_search_query = bpy.props.StringProperty(
        name=SEARCH_INSTALLED_LABEL
    )

    # Register background update timer
    if not bpy.app.timers.is_registered(bg_update_check):
        bpy.app.timers.register(bg_update_check, first_interval=60.0)


def unregister():
    """Unregister all classes and properties."""
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.search_query
    del bpy.types.Scene.bulk_download_path
    del bpy.types.Scene.package_list
    del bpy.types.Scene.installed_package_list
    del bpy.types.Scene.show_search_results
    del bpy.types.Scene.show_installed_packages
    del bpy.types.Scene.installed_search_query

    # Unregister timers
    if bpy.app.timers.is_registered(bg_update_check):
        bpy.app.timers.unregister(bg_update_check)


if __name__ == "__main__" and hasattr(bpy.utils, "register_class"):
    register()
